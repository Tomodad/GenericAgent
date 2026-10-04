import argparse, asyncio, glob, importlib, json, os, queue as Q, re, socket, sys, threading, time, uuid
import atexit, hashlib
from pathlib import Path
try:
    import msvcrt
except Exception:
    msvcrt = None

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import traceback
import lark_oapi as lark
from lark_oapi.api.im.v1 import *


def _ensure_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _workspace_root_dir():
    root = os.environ.get("GA_WORKSPACE_ROOT")
    if root:
        return _ensure_dir(Path(root).expanduser().resolve())
    return _ensure_dir(Path(PROJECT_ROOT).resolve())


def _workspace_config_dir(root=None):
    base = Path(root).expanduser().resolve() if root else _workspace_root_dir()
    if base.name == "ga_config":
        return _ensure_dir(base)
    return _ensure_dir(base / "ga_config")


def _load_dict_config(path):
    path = Path(path)
    if not path.exists():
        return None
    try:
        if path.suffix == ".py":
            mod_name = f"_fs_mykey_{uuid.uuid4().hex}"
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if not spec or not spec.loader:
                return None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            data = {k: v for k, v in vars(module).items() if not k.startswith("_")}
        else:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception as e:
        print(f"[ERROR] load config failed {path}: {e}")
        return None


def _resolve_mykey_path():
    workspace_root = _workspace_root_dir()
    config_root = _workspace_config_dir(workspace_root)
    candidates = [
        config_root / "mykey.json",
        config_root / "mykey.py",
        workspace_root / "mykey.json",
        workspace_root / "mykey.py",
        Path(PROJECT_ROOT) / "mykey.json",
        Path(PROJECT_ROOT) / "mykey.py",
    ]
    for candidate in candidates:
        if _load_dict_config(candidate):
            return candidate
    return candidates[0]


def _ensure_runtime_paths():
    workspace_root = _workspace_root_dir()
    config_root = _workspace_config_dir(workspace_root)
    os.environ.setdefault("GA_WORKSPACE_ROOT", str(workspace_root))
    os.environ.setdefault("GA_USER_DATA_DIR", str(config_root))
    return str(workspace_root), str(config_root)


_ensure_runtime_paths()
from agentmain import GeneraticAgent
from frontends.chatapp_common import AgentChatMixin, FILE_HINT, split_text

_TAG_PATS = [r"<" + t + r">.*?</" + t + r">" for t in ("thinking", "summary", "tool_use", "file_content")]
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".tif"}
_AUDIO_EXTS = {".opus", ".mp3", ".wav", ".m4a", ".aac"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
_FILE_TYPE_MAP = {
    ".opus": "opus",
    ".mp4": "mp4",
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "doc",
    ".xls": "xls",
    ".xlsx": "xls",
    ".ppt": "ppt",
    ".pptx": "ppt",
}
_MSG_TYPE_MAP = {"image": "[image]", "audio": "[audio]", "file": "[file]", "media": "[media]", "sticker": "[sticker]"}

TEMP_DIR = os.path.join(PROJECT_ROOT, "temp")
MEDIA_DIR = os.path.join(TEMP_DIR, "feishu_media")
os.makedirs(MEDIA_DIR, exist_ok=True)


_TRUNC_TAIL = 300  # 截断兜底时保留原文尾部字符数
_DEDUP_TTL_SEC = 10 * 60
_DEDUP_MAX = 2000
_DEDUP_LOCK = threading.Lock()
_SEEN_MESSAGES = {}


def _claim_message_once(message_id):
    """Best-effort cross-platform dedup for Feishu reconnect redeliveries."""
    if not message_id:
        return True
    now = time.time()
    with _DEDUP_LOCK:
        expired = [mid for mid, ts in _SEEN_MESSAGES.items() if now - ts > _DEDUP_TTL_SEC]
        for mid in expired:
            _SEEN_MESSAGES.pop(mid, None)
        if len(_SEEN_MESSAGES) > _DEDUP_MAX:
            for mid, _ in sorted(_SEEN_MESSAGES.items(), key=lambda item: item[1])[:len(_SEEN_MESSAGES) - _DEDUP_MAX]:
                _SEEN_MESSAGES.pop(mid, None)
        if message_id in _SEEN_MESSAGES:
            return False
        _SEEN_MESSAGES[message_id] = now
        return True


def _clean(text):
    for pat in _TAG_PATS:
        text = re.sub(pat, "", text or "", flags=re.DOTALL)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _extract_files(text):
    return re.findall(r"\[FILE:([^\]]+)\]", text or "")


def _strip_files(text):
    return re.sub(r"\[FILE:[^\]]+\]", "", text or "").strip()


def _display_text(text):
    raw = text or ""
    cleaned = _strip_files(_clean(raw))
    if cleaned:
        return cleaned
    summary_parts = []
    for part in re.findall(r"<summary\b[^>]*>(.*?)</summary\s*>", raw, flags=re.DOTALL | re.IGNORECASE):
        plain = _strip_files(re.sub(r"<[^>]+>", "", part)).strip()
        if plain:
            summary_parts.append(plain)
    if summary_parts:
        return "\n".join(summary_parts)
    tail = raw.strip()[-_TRUNC_TAIL:]
    return "⚠️ 模型输出被截断或为空" + (f"\n…{tail}" if tail else "")


def _to_allowed_set(value):
    if value is None:
        return set()
    if isinstance(value, str):
        value = [value]
    return {str(x).strip() for x in value if str(x).strip()}


def _parse_json(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _extract_share_card_content(content_json, msg_type):
    parts = []
    if msg_type == "share_chat":
        parts.append(f"[shared chat: {content_json.get('chat_id', '')}]")
    elif msg_type == "share_user":
        parts.append(f"[shared user: {content_json.get('user_id', '')}]")
    elif msg_type == "interactive":
        parts.extend(_extract_interactive_content(content_json))
    elif msg_type == "share_calendar_event":
        parts.append(f"[shared calendar event: {content_json.get('event_key', '')}]")
    elif msg_type == "system":
        parts.append("[system message]")
    elif msg_type == "merge_forward":
        parts.append("[merged forward messages]")
    return "\n".join([p for p in parts if p]).strip() or f"[{msg_type}]"


def _extract_interactive_content(content):
    parts = []
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            return [content] if content.strip() else []
    if not isinstance(content, dict):
        return parts
    title = content.get("title")
    if isinstance(title, dict):
        title_text = title.get("content", "") or title.get("text", "")
        if title_text:
            parts.append(f"title: {title_text}")
    elif isinstance(title, str) and title:
        parts.append(f"title: {title}")
    elements = content.get("elements", [])
    if isinstance(elements, list):
        for row in elements:
            if isinstance(row, dict):
                parts.extend(_extract_element_content(row))
            elif isinstance(row, list):
                for el in row:
                    parts.extend(_extract_element_content(el))
    card = content.get("card", {})
    if card:
        parts.extend(_extract_interactive_content(card))
    header = content.get("header", {})
    if isinstance(header, dict):
        header_title = header.get("title", {})
        if isinstance(header_title, dict):
            header_text = header_title.get("content", "") or header_title.get("text", "")
            if header_text:
                parts.append(f"title: {header_text}")
    return [p for p in parts if p]


def _extract_element_content(element):
    parts = []
    if not isinstance(element, dict):
        return parts
    tag = element.get("tag", "")
    if tag in ("markdown", "lark_md"):
        content = element.get("content", "")
        if content:
            parts.append(content)
    elif tag == "div":
        text = element.get("text", {})
        if isinstance(text, dict):
            text_content = text.get("content", "") or text.get("text", "")
            if text_content:
                parts.append(text_content)
        elif isinstance(text, str) and text:
            parts.append(text)
        for field in element.get("fields", []) or []:
            if isinstance(field, dict):
                field_text = field.get("text", {})
                if isinstance(field_text, dict):
                    content = field_text.get("content", "") or field_text.get("text", "")
                    if content:
                        parts.append(content)
    elif tag == "a":
        href = element.get("href", "")
        text = element.get("text", "")
        if href:
            parts.append(f"link: {href}")
        if text:
            parts.append(text)
    elif tag == "button":
        text = element.get("text", {})
        if isinstance(text, dict):
            content = text.get("content", "") or text.get("text", "")
            if content:
                parts.append(content)
        url = element.get("url", "") or (element.get("multi_url", {}) or {}).get("url", "")
        if url:
            parts.append(f"link: {url}")
    elif tag == "img":
        alt = element.get("alt", {})
        if isinstance(alt, dict):
            parts.append(alt.get("content", "[image]") or "[image]")
        else:
            parts.append("[image]")
    for child in element.get("elements", []) or []:
        parts.extend(_extract_element_content(child))
    for col in element.get("columns", []) or []:
        for child in (col.get("elements", []) if isinstance(col, dict) else []):
            parts.extend(_extract_element_content(child))
    return parts


def _extract_post_content(content_json):
    def _parse_block(block):
        if not isinstance(block, dict) or not isinstance(block.get("content"), list):
            return None, []
        texts, images = [], []
        if block.get("title"):
            texts.append(block.get("title"))
        for row in block["content"]:
            if not isinstance(row, list):
                continue
            for el in row:
                if not isinstance(el, dict):
                    continue
                tag = el.get("tag")
                if tag in ("text", "a"):
                    texts.append(el.get("text", ""))
                elif tag == "at":
                    texts.append(f"@{el.get('user_name', 'user')}")
                elif tag == "img" and el.get("image_key"):
                    images.append(el["image_key"])
        text = " ".join([t for t in texts if t]).strip()
        return text or None, images

    root = content_json
    if isinstance(root, dict) and isinstance(root.get("post"), dict):
        root = root["post"]
    if not isinstance(root, dict):
        return "", []
    if "content" in root:
        text, imgs = _parse_block(root)
        if text or imgs:
            return text or "", imgs
    for key in ("zh_cn", "en_us", "ja_jp"):
        if key in root:
            text, imgs = _parse_block(root[key])
            if text or imgs:
                return text or "", imgs
    for val in root.values():
        if isinstance(val, dict):
            text, imgs = _parse_block(val)
            if text or imgs:
                return text or "", imgs
    return "", []


AGENT_IDLE_TIMEOUT_SEC = max(1, int(os.getenv("FSAPP_AGENT_IDLE_TIMEOUT_SEC", "900")))
AGENT_MAX_RUNTIME_SEC = max(AGENT_IDLE_TIMEOUT_SEC, int(os.getenv("FSAPP_AGENT_MAX_RUNTIME_SEC", "7200")))

agent = None
agent_error = None
agent_thread = None
client, user_tasks, app = None, {}, None
agent_lock = threading.Lock()


def _load_config():
    path = _resolve_mykey_path()
    if not path or not path.exists():
        return {}, str(path or "")
    try:
        data = _load_dict_config(path)
        return data if isinstance(data, dict) else {}, str(path)
    except Exception as e:
        print(f"[ERROR] load mykey failed {path}: {e}")
        return {}, str(path)


def _feishu_config():
    cfg, path = _load_config()
    app_id = str(cfg.get("fs_app_id", "") or "").strip()
    app_secret = str(cfg.get("fs_app_secret", "") or "").strip()
    allowed = _to_allowed_set(cfg.get("fs_allowed_users", []))
    # 用户可配置 fs_app_domain 切换国际版 Lark (larksuite.com)。
    # 未配置时 fallback = lark.LARK_DOMAIN (国内 feishu.cn), 保持默认行为。
    raw_domain = cfg.get("fs_app_domain", "") or ""
    raw_domain = str(raw_domain).strip()
    if raw_domain:
        domain = raw_domain
    else:
        domain = lark.LARK_DOMAIN
    return app_id, app_secret, allowed, (not allowed or "*" in allowed), path, domain


APP_ID, APP_SECRET, ALLOWED_USERS, PUBLIC_ACCESS, CONFIG_PATH, APP_DOMAIN = _feishu_config()


def get_agent():
    global agent, agent_error, agent_thread
    with agent_lock:
        if agent is not None:
            return agent
        if agent_error:
            raise RuntimeError(agent_error)
        try:
            agent = GeneraticAgent()
            agent_thread = threading.Thread(target=agent.run, daemon=True)
            agent_thread.start()
            return agent
        except Exception as e:
            agent_error = str(e)
            raise


def create_client():
    return lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).log_level(lark.LogLevel.INFO).domain(APP_DOMAIN).build()


def _mask_secret(value):
    value = str(value or "")
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


def check_config(init_agent=False):
    app_id, app_secret, allowed, public_access, path = _feishu_config()
    result = {
        "config_path": path,
        "app_id": app_id,
        "app_secret": _mask_secret(app_secret),
        "app_secret_present": bool(app_secret),
        "public_access": public_access,
        "allowed_users": sorted(allowed),
        "ready": bool(app_id and app_secret),
    }
    if init_agent:
        try:
            ga = get_agent()
            result["agent_ready"] = True
            result["llm_count"] = len(ga.list_llms()) if hasattr(ga, "list_llms") else 0
            result["current_llm"] = ga.get_llm_name() if getattr(ga, "llmclient", None) else ""
        except Exception as e:
            result["agent_ready"] = False
            result["agent_error"] = str(e)
    return result


def _card_raw(elements):
    return json.dumps({
        "schema": "2.0",
        "config": {"streaming_mode": False, "width_mode": "fill"},
        "body": {"elements": elements},
    }, ensure_ascii=False)


def _card(text):
    return _card_raw([{"tag": "markdown", "content": text}])


def _send_raw(receive_id, payload, msg_type, rtype):
    try:
        body = CreateMessageRequest.builder().receive_id_type(rtype).request_body(
            CreateMessageRequestBody.builder().receive_id(receive_id).msg_type(msg_type).content(payload).build()
        ).build()
        r = client.im.v1.message.create(body)
        if r.success():
            return r.data.message_id if r.data else None
        print(f"发送失败: {r.code}, {r.msg}")
    except Exception as e:
        print(f"[ERROR] send_message failed: {e}")
        traceback.print_exc()
    return None


# 不可重写失败的错误码（命中后msg_id不可信，需fallback）
_PATCH_FATAL_CODES = {230099, 230020, 230021, 99991663, 99991672, 230040, 230041}
# PatchMessage 404（message_id被删/撤回/跨群）
_PATCH_NOT_FOUND = {404}


def _patch_card(message_id, card_json):
    """Patch 飞书卡片。返回 True=ok, False=失败, None=不可重试(已删/超限, 需fallback新建)."""
    try:
        body = PatchMessageRequest.builder().message_id(message_id).request_body(
            PatchMessageRequestBody.builder().content(card_json).build()
        ).build()
        r = client.im.v1.message.patch(body)
        if r.success():
            return True
        code = getattr(r, "code", None) or 0
        msg = getattr(r, "msg", "")
        print(f"[ERROR] patch_card 失败: code={code}, msg={msg}")
        # 命中 fatal 集合: 卡片已不可信, 需重置msg_id走fallback
        if code in _PATCH_FATAL_CODES or code in _PATCH_NOT_FOUND or code >= 400:
            return None
        return False
    except Exception as e:
        print(f"[ERROR] patch_card exception: {e}")
        traceback.print_exc()
        return False


def send_message(receive_id, content, msg_type="text", use_card=False, receive_id_type="open_id"):
    if use_card:
        return _send_raw(receive_id, _card(content), "interactive", receive_id_type)
    if msg_type == "text":
        return _send_raw(receive_id, json.dumps({"text": content}, ensure_ascii=False), "text", receive_id_type)
    return _send_raw(receive_id, content, msg_type, receive_id_type)


def update_message(message_id, content):
    return _patch_card(message_id, _card(content))


def _upload_image_sync(file_path):
    try:
        with open(file_path, "rb") as f:
            request = CreateImageRequest.builder().request_body(
                CreateImageRequestBody.builder().image_type("message").image(f).build()
            ).build()
            response = client.im.v1.image.create(request)
            if response.success():
                return response.data.image_key
            print(f"[ERROR] upload image failed: {response.code}, {response.msg}")
    except Exception as e:
        print(f"[ERROR] upload image failed {file_path}: {e}")
    return None


def _upload_file_sync(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    file_type = _FILE_TYPE_MAP.get(ext, "stream")
    file_name = os.path.basename(file_path)
    try:
        with open(file_path, "rb") as f:
            request = CreateFileRequest.builder().request_body(
                CreateFileRequestBody.builder().file_type(file_type).file_name(file_name).file(f).build()
            ).build()
            response = client.im.v1.file.create(request)
            if response.success():
                return response.data.file_key
            print(f"[ERROR] upload file failed: {response.code}, {response.msg}")
    except Exception as e:
        print(f"[ERROR] upload file failed {file_path}: {e}")
    return None


def _download_image_sync(message_id, image_key):
    try:
        request = GetMessageResourceRequest.builder().message_id(message_id).file_key(image_key).type("image").build()
        response = client.im.v1.message_resource.get(request)
        if response.success():
            data = response.file.read() if hasattr(response.file, "read") else response.file
            return data, response.file_name
        print(f"[ERROR] download image failed: {response.code}, {response.msg}")
    except Exception as e:
        print(f"[ERROR] download image failed {image_key}: {e}")
    return None, None


def _download_file_sync(message_id, file_key, resource_type="file"):
    if resource_type == "audio":
        resource_type = "file"
    try:
        request = GetMessageResourceRequest.builder().message_id(message_id).file_key(file_key).type(resource_type).build()
        response = client.im.v1.message_resource.get(request)
        if response.success():
            data = response.file.read() if hasattr(response.file, "read") else response.file
            return data, response.file_name
        print(f"[ERROR] download {resource_type} failed: {response.code}, {response.msg}")
    except Exception as e:
        print(f"[ERROR] download {resource_type} failed {file_key}: {e}")
    return None, None


def _download_and_save_media(msg_type, content_json, message_id):
    data, filename = None, None
    if msg_type == "image":
        image_key = content_json.get("image_key")
        if image_key and message_id:
            data, filename = _download_image_sync(message_id, image_key)
            if not filename:
                filename = f"{image_key[:16]}.jpg"
    elif msg_type in ("audio", "file", "media"):
        file_key = content_json.get("file_key")
        if file_key and message_id:
            data, filename = _download_file_sync(message_id, file_key, msg_type)
            if not filename:
                filename = file_key[:16]
            if msg_type == "audio" and filename and not filename.endswith(".opus"):
                filename = f"{filename}.opus"
    if data and filename:
        file_path = os.path.join(MEDIA_DIR, os.path.basename(filename))
        with open(file_path, "wb") as f:
            f.write(data)
        return file_path, filename
    return None, None


def _describe_media(msg_type, file_path, filename):
    if msg_type == "image":
        return f"[image: {filename}]\n[Image: source: {file_path}]"
    if msg_type == "audio":
        return f"[audio: {filename}]\n[File: source: {file_path}]"
    if msg_type in ("file", "media"):
        return f"[{msg_type}: {filename}]\n[File: source: {file_path}]"
    return f"[{msg_type}]\n[File: source: {file_path}]"


def _resolve_generated_file_path(file_path, task_cwd=None):
    """解析 [FILE:] 路径；相对路径优先按 Agent 任务 cwd，再尝试项目根目录。"""
    raw = os.path.expandvars(os.path.expanduser(str(file_path or "").strip().strip('"').strip("'")))
    if not raw:
        return ""
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    roots = [task_cwd, os.path.join(PROJECT_ROOT, "temp"), PROJECT_ROOT]
    candidates = []
    seen = set()
    for root in roots:
        if not root:
            continue
        candidate = os.path.abspath(os.path.join(root, raw))
        key = os.path.normcase(candidate)
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)
            if os.path.isfile(candidate):
                return candidate
    return candidates[0] if candidates else os.path.abspath(raw)


def _send_local_file(receive_id, file_path, receive_id_type="open_id", task_cwd=None):
    resolved_path = _resolve_generated_file_path(file_path, task_cwd)
    if not os.path.isfile(resolved_path):
        send_message(receive_id, f"⚠️ 文件不存在: {file_path}", receive_id_type=receive_id_type)
        return False
    file_path = resolved_path
    ext = os.path.splitext(file_path)[1].lower()
    if ext in _IMAGE_EXTS:
        image_key = _upload_image_sync(file_path)
        if image_key:
            send_message(receive_id, json.dumps({"image_key": image_key}, ensure_ascii=False), msg_type="image", receive_id_type=receive_id_type)
            return True
    else:
        file_key = _upload_file_sync(file_path)
        if file_key:
            msg_type = "media" if ext in _AUDIO_EXTS or ext in _VIDEO_EXTS else "file"
            send_message(receive_id, json.dumps({"file_key": file_key}, ensure_ascii=False), msg_type=msg_type, receive_id_type=receive_id_type)
            return True
    send_message(receive_id, f"⚠️ 文件发送失败: {os.path.basename(file_path)}", receive_id_type=receive_id_type)
    return False


def _send_generated_files(receive_id, raw_text, receive_id_type="open_id", task_cwd=None):
    for file_path in _extract_files(raw_text):
        _send_local_file(receive_id, file_path, receive_id_type, task_cwd=task_cwd)


def _build_user_message(message):
    msg_type = message.message_type
    message_id = message.message_id
    content_json = _parse_json(message.content)
    parts, image_paths = [], []
    if msg_type == "text":
        text = str(content_json.get("text", "") or "").strip()
        if text:
            parts.append(text)
    elif msg_type == "post":
        text, image_keys = _extract_post_content(content_json)
        if text:
            parts.append(text)
        for image_key in image_keys:
            file_path, filename = _download_and_save_media("image", {"image_key": image_key}, message_id)
            if file_path and filename:
                parts.append(_describe_media("image", file_path, filename))
                image_paths.append(file_path)
            else:
                parts.append("[image: download failed]")
    elif msg_type in ("image", "audio", "file", "media"):
        file_path, filename = _download_and_save_media(msg_type, content_json, message_id)
        if file_path and filename:
            parts.append(_describe_media(msg_type, file_path, filename))
            if msg_type == "image":
                image_paths.append(file_path)
        else:
            parts.append(f"[{msg_type}: download failed]")
    elif msg_type in ("share_chat", "share_user", "interactive", "share_calendar_event", "system", "merge_forward"):
        parts.append(_extract_share_card_content(content_json, msg_type))
    else:
        parts.append(_MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"))
    return "\n".join([p for p in parts if p]).strip(), image_paths


def _fmt_tool_call(tc):
    name = tc.get('tool_name', '?')
    args = {k: v for k, v in (tc.get('args') or {}).items() if not k.startswith('_')}
    return f"- `{name}`({json.dumps(args, ensure_ascii=False)[:200]})"


def _build_step_detail(resp, tool_calls):
    """从 LLM response + tool_calls 组装单步展开详情（纯函数）。"""
    parts = []
    thinking = (getattr(resp, 'thinking', '') or '').strip() if resp else ''
    if thinking:
        parts.append(f"### 💭 Thinking\n{thinking}")
    if tool_calls:
        parts.append("### 🛠 Tool Calls\n" + "\n".join(_fmt_tool_call(tc) for tc in tool_calls))
    # 中间轮允许 content 只有 <summary> 等控制标签；summary 已作为 panel 标题展示，
    # 清洗后为空不等于模型被截断。终态仍由 _display_text 保留真正空输出的告警。
    raw_content = (getattr(resp, 'content', '') or '') if resp else ''
    content = _strip_files(_clean(raw_content))
    if content and content != '...':
        parts.append(f"### 📝 Output\n{content}")
    return "\n\n".join(parts)


def _select_terminal_output(item):
    """正常完成时只展示最后一轮输出；异常附加文本或旧协议保留完整 done。"""
    if not isinstance(item, dict):
        return str(item or "")
    full = str(item.get("done", "") or "")
    outputs = item.get("outputs")
    if isinstance(outputs, (list, tuple)):
        last = next((x for x in reversed(outputs) if isinstance(x, str) and x.strip()), "")
        # agentmain 的正常 done 以最后一轮 output 结尾；异常会在其后追加错误块。
        if last and full.rstrip().endswith(last.rstrip()):
            return last
    return full


class _TaskCard:
    """飞书任务卡片：单卡片持续 patch；每步一个独立折叠面板。
    修复:
    - _DETAIL_LIMIT 8000 → 28000 (单 step 内容足够长不用硬切)
    - 超长 step 自动拆成多个 collapsible_panel (按 ~26000 切)
    - _push 失败 fatal (None) 时把 self.msg_id 置 None 并 _broken=True,
      后续 step 全部走 send_message(text) + split_text, 不再尝试 patch
    - 只 push 一次失败，后续不再重试 patch (避免重试风暴触发限流)
    """
    _DETAIL_LIMIT = 28000     # 单 panel markdown 防御性上限
    _PANEL_CHUNK = 9000       # 单 panel 的 UTF-8 字节目标，给 JSON 转义和卡片结构留足余量
    _BODY_LIMIT = 30000       # 整卡片序列化字节上限经验值 (Lark ~30KB)

    def __init__(self, receive_id, rid_type):
        self.rid, self.rtype = receive_id, rid_type
        self.steps = []          # [(turn_no, summary, detail, part_no, total_parts), ...]，仅保存当前卡
        self.turn_count = 0
        self.page_no = 1
        self.status = "🤔 思考中..."
        self.final = None
        self.msg_id = None
        self._broken = False     # 卡片推送失败后切换为 text-only 模式
        self._summary_fallback_turns = set()
        self.start_fallback_sent = False
        self.final_fallback_sent = False

    def _split_long_detail(self, detail):
        """按 UTF-8 字节切 detail，优先在段落边界断开，保证单 panel 不挤爆整卡。"""
        rest = str(detail or "")
        if not rest:
            return [""]
        parts = []
        while len(rest.encode("utf-8")) > self._PANEL_CHUNK:
            lo, hi = 1, len(rest)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if len(rest[:mid].encode("utf-8")) <= self._PANEL_CHUNK:
                    lo = mid
                else:
                    hi = mid - 1
            cut = rest.rfind("\n\n", max(1, lo // 2), lo + 1)
            if cut < 1:
                cut = lo
            parts.append(rest[:cut].rstrip())
            rest = rest[cut:].lstrip("\n")
        parts.append(rest)
        return parts

    def _step_panel(self, turn_no, summary, detail, part_no=1, total_parts=1):
        suffix = f" (续{part_no}/{total_parts})" if total_parts > 1 else ""
        if len(detail) > self._DETAIL_LIMIT:
            detail = detail[:self._DETAIL_LIMIT] + f"\n\n…(已截断,共 {len(detail)} 字符)"
        return {
            "tag": "collapsible_panel", "expanded": False,
            "header": {"title": {"tag": "plain_text", "content": f"Turn {turn_no} · {summary}{suffix}"}},
            "elements": [{"tag": "markdown", "content": detail or "_(无输出)_"}],
        }

    def _build(self):
        els = [{"tag": "markdown", "content": f"**{self.status}**"}]
        for turn_no, summary, detail, part_no, total_parts in self.steps:
            els.append(self._step_panel(turn_no, summary, detail, part_no, total_parts))
        if self.final:
            els += [{"tag": "hr"}, {"tag": "markdown", "content": self.final}]
        return _card_raw(els)

    def _card_size(self):
        return len(self._build().encode("utf-8"))

    def _rollover(self):
        """开始新的进度卡；旧卡保留为历史页，不再把整段过程降级成普通消息。"""
        self.steps = []
        self.final = None
        self.msg_id = None
        self.page_no += 1

    def _send_step_summary(self, turn_no, summary):
        """卡片不可用时只发 step 摘要，禁止把完整 thinking/tool detail 泄露为普通消息。"""
        if turn_no in self._summary_fallback_turns:
            return
        self._summary_fallback_turns.add(turn_no)
        send_message(self.rid, f"⏳ Turn {turn_no} · {summary}", receive_id_type=self.rtype)

    def _push(self):
        if self._broken:
            return False
        card = self._build()
        card_size = len(card.encode("utf-8"))
        if card_size > self._BODY_LIMIT:
            self._broken = True
            print(f"[TaskCard] unexpected card size {card_size}B > {self._BODY_LIMIT}B, switch to summary-only")
            return False
        if self.msg_id:
            ok = _patch_card(self.msg_id, card)
        else:
            self.msg_id = _send_raw(self.rid, card, "interactive", self.rtype)
            ok = bool(self.msg_id)
        if ok is None or ok is False:
            prev_id = self.msg_id
            self.msg_id = None
            self._broken = True
            print(f"[TaskCard] patch/send failed (id={prev_id}), switch to summary-only mode")
            return False
        return True

    def _fallback_text(self, text, *, final=False):
        attr = "final_fallback_sent" if final else "start_fallback_sent"
        if getattr(self, attr):
            return
        setattr(self, attr, True)
        if text:
            for part in split_text(text, 3500):
                send_message(self.rid, part, receive_id_type=self.rtype)

    # ── 公开接口 ──

    def start(self):
        if not self._push():
            self._fallback_text("🤔 思考中...")

    def step(self, summary, detail=""):
        self.turn_count += 1
        turn_no = self.turn_count
        summary = str(summary or "本轮已完成")
        if len(summary) > 500:
            summary = summary[:500] + "…"
        if self._broken:
            self._send_step_summary(turn_no, summary)
            return

        chunks = self._split_long_detail(detail)
        total_parts = len(chunks)
        for part_no, chunk in enumerate(chunks, 1):
            record = (turn_no, summary, chunk, part_no, total_parts)
            self.status = f"⏳ 工作中 · Turn {turn_no} · 进度页 {self.page_no}"
            self.steps.append(record)
            if self._card_size() > self._BODY_LIMIT:
                self.steps.pop()
                if self.steps:
                    self._rollover()
                    self.status = f"⏳ 工作中 · Turn {turn_no} · 进度页 {self.page_no}"
                    self.steps.append(record)
            if not self._push():
                self._send_step_summary(turn_no, summary)
                return

    def done(self, text):
        final_text = _display_text(text) or "_(无文本输出)_"
        self.status = "✅ 已完成"
        self.final = final_text
        if self._broken:
            self._fallback_text(final_text, final=True)
            return
        if self._card_size() > self._BODY_LIMIT and self.steps:
            self._rollover()
            self.status = "✅ 已完成"
            self.final = final_text
        if self._card_size() > self._BODY_LIMIT:
            self.final = None
            self._fallback_text(final_text, final=True)
            return
        if not self._push():
            self._fallback_text(final_text, final=True)

    def fail(self, msg):
        fail_text = f"❌ {msg}"
        self.status = fail_text
        if self._broken:
            self._fallback_text(fail_text, final=True)
            return
        if self._card_size() > self._BODY_LIMIT and self.steps:
            self._rollover()
            self.status = fail_text
        if not self._push():
            self._fallback_text(fail_text, final=True)


def _make_task_hook(card, task_id, on_progress=None):
    """飞书任务 hook：记录每轮进展；终态控制可由调用方接管。"""
    def hook(ctx):
        try:
            parent = getattr(ctx.get("self"), "parent", None)
            if getattr(parent, "_fs_active_task_id", None) != task_id:
                return
            summary = ctx.get("summary")
            if summary:
                detail = _build_step_detail(ctx.get("response"), ctx.get("tool_calls") or [])
                if on_progress:
                    on_progress(summary, detail)
                else:
                    card.step(summary, detail)
        except Exception as e:
            print(f"[fs hook] error: {e}")
    return hook


class FeishuApp(AgentChatMixin):
    label, source, split_limit = "Feishu", "feishu", 3500

    async def send_text(self, chat_id, content, *, receive_id=None, receive_id_type="open_id", **_):
        rid = receive_id or chat_id
        for part in split_text(content, self.split_limit):
            await asyncio.to_thread(send_message, rid, part, "text", False, receive_id_type)

    async def send_done(self, chat_id, raw_text, *, receive_id=None, receive_id_type="open_id", **_):
        rid = receive_id or chat_id
        text = _display_text(raw_text)
        await asyncio.to_thread(send_message, rid, text, "text", False, receive_id_type)
        await asyncio.to_thread(_send_generated_files, rid, raw_text, receive_id_type)

    async def run_agent(self, chat_id, text, *, receive_id=None, receive_id_type="open_id", images=None, **_):
        if self.user_tasks:
            await self.send_text(chat_id, "当前会话已有任务在运行，请等待完成或发送 /stop 后再试。", receive_id=receive_id, receive_id_type=receive_id_type)
            return
        state = {"running": True}
        self.user_tasks[chat_id] = state
        rid = receive_id or chat_id
        task_id = f"{chat_id}_{uuid.uuid4().hex}"
        hook_key = f"fs_{task_id}"
        card = _TaskCard(rid, receive_id_type)
        started_at = time.monotonic()
        result = {"raw": None, "terminal": None, "last_progress": started_at}
        finish_lock = threading.Lock()

        def _is_terminal():
            with finish_lock:
                return result["terminal"] is not None

        def _progress(summary, detail):
            with finish_lock:
                if result["terminal"] is not None:
                    return False
                result["last_progress"] = time.monotonic()
                card.step(summary, detail)
                return True

        def _finish(item):
            raw = str(item.get("done", "") or "") if isinstance(item, dict) else str(item or "")
            terminal_output = _select_terminal_output(item)
            with finish_lock:
                if result["terminal"] is not None:
                    return False
                result["raw"] = raw
                result["terminal"] = "done"
                card.done(_display_text(terminal_output))
                handler = getattr(self.agent, "handler", None)
                task_cwd = getattr(handler, "cwd", None)
                _send_generated_files(rid, raw, receive_id_type=receive_id_type, task_cwd=task_cwd)
                return True

        def _fail(msg):
            with finish_lock:
                if result["terminal"] is not None:
                    return False
                result["terminal"] = "failed"
                card.fail(msg)
                return True

        try:
            await asyncio.to_thread(card.start)
            if not hasattr(self.agent, '_turn_end_hooks'):
                self.agent._turn_end_hooks = {}
            self.agent._turn_end_hooks[hook_key] = _make_task_hook(card, task_id, _progress)
            self.agent._fs_active_task_id = task_id
            dq = self.agent.put_task(f"{FILE_HINT}\n\n{text}", source=self.source, images=images or None)
            while state["running"] and not _is_terminal():
                try:
                    item = await asyncio.to_thread(dq.get, True, 1)
                except Q.Empty:
                    item = None
                if item and "done" in item:
                    await asyncio.to_thread(_finish, item)
                    break
                now = time.monotonic()
                with finish_lock:
                    idle_for = now - result["last_progress"]
                if now - started_at > AGENT_MAX_RUNTIME_SEC:
                    self.agent.abort()
                    await asyncio.to_thread(_fail, f"任务超过最长运行时间（{AGENT_MAX_RUNTIME_SEC}秒）")
                    break
                if idle_for > AGENT_IDLE_TIMEOUT_SEC:
                    self.agent.abort()
                    await asyncio.to_thread(_fail, f"任务长时间无进展（{AGENT_IDLE_TIMEOUT_SEC}秒）")
                    break
            if not state["running"] and not _is_terminal():
                self.agent.abort()
                await asyncio.to_thread(_fail, "已停止")
        except Exception as e:
            traceback.print_exc()
            await asyncio.to_thread(_fail, f"错误: {e}")
        finally:
            if getattr(self.agent, "_fs_active_task_id", None) == task_id:
                try:
                    delattr(self.agent, "_fs_active_task_id")
                except AttributeError:
                    pass
            if hasattr(self.agent, '_turn_end_hooks'):
                self.agent._turn_end_hooks.pop(hook_key, None)
            self.user_tasks.pop(chat_id, None)


def get_app():
    global app
    if app is None:
        app = FeishuApp(get_agent(), user_tasks)
    return app


def _run_async(coro):
    try:
        asyncio.run(coro)
    except Exception:
        traceback.print_exc()


def handle_message(data):
    event, message, sender = data.event, data.event.message, data.event.sender
    message_id = getattr(message, "message_id", "") or ""
    if not _claim_message_once(message_id):
        print(f"忽略重复飞书消息: {message_id}")
        return
    open_id = sender.sender_id.open_id
    chat_id = message.chat_id
    if not PUBLIC_ACCESS and open_id not in ALLOWED_USERS:
        print(f"未授权用户: {open_id}")
        return
    user_input, image_paths = _build_user_message(message)
    if not user_input:
        if chat_id:
            send_message(chat_id, f"⚠️ 暂不支持处理此类飞书消息：{message.message_type}", receive_id_type="chat_id")
        else:
            send_message(open_id, f"⚠️ 暂不支持处理此类飞书消息：{message.message_type}")
        return
    print(f"收到消息 [{open_id}] ({message.message_type}, {len(image_paths)} images): {user_input[:200]}")
    receive_id = chat_id or open_id
    receive_id_type = "chat_id" if chat_id else "open_id"
    chat_key = receive_id
    if message.message_type == "text" and user_input.startswith("/"):
        threading.Thread(
            target=_run_async,
            args=(get_app().handle_command(chat_key, user_input, receive_id=receive_id, receive_id_type=receive_id_type),),
            daemon=True,
        ).start()
        return
    threading.Thread(
        target=_run_async,
        args=(get_app().run_agent(chat_key, user_input, receive_id=receive_id, receive_id_type=receive_id_type, images=image_paths),),
        daemon=True,
    ).start()


def main():
    global client, APP_ID, APP_SECRET, ALLOWED_USERS, PUBLIC_ACCESS, CONFIG_PATH, APP_DOMAIN
    APP_ID, APP_SECRET, ALLOWED_USERS, PUBLIC_ACCESS, CONFIG_PATH, APP_DOMAIN = _feishu_config()
    if not APP_ID or not APP_SECRET:
        print(f"错误: 请在 mykey 配置中填写 fs_app_id 和 fs_app_secret\n配置文件: {CONFIG_PATH}", flush=True)
        sys.exit(1)
    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(handle_message).build()
    retry_delay = 5
    while True:
        try:
            client = create_client()
            cli = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler, log_level=lark.LogLevel.INFO, domain=APP_DOMAIN)
            print("=" * 50 + "\n飞书 Agent 已启动（长连接模式）\n" + f"App ID: {APP_ID}\n配置: {CONFIG_PATH}\n等待消息...\n" + "=" * 50, flush=True)
            cli.start()
            retry_delay = 5
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[WARN] 飞书长连接断开或启动失败: {e}", flush=True)
            traceback.print_exc()
        print(f"[INFO] {retry_delay}s 后重连飞书长连接...", flush=True)
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 120)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="A3Agent Feishu frontend")
    parser.add_argument("--check", action="store_true", help="只检查飞书配置，不启动长连接")
    parser.add_argument("--check-agent", action="store_true", help="检查配置并初始化 Agent/LLM")
    args = parser.parse_args()
    if args.check or args.check_agent:
        print(json.dumps(check_config(init_agent=args.check_agent), ensure_ascii=False, indent=2), flush=True)
    else:
        try:
            _fsapp_lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            _fsapp_lock.bind(("127.0.0.1", 19532))
        except OSError:
            print("[Feishu] Another instance running, exiting.")
            sys.exit(1)
        main()
