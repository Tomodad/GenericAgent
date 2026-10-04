"""
MiniMax 多模态生成工具集
========================
统一封装 MiniMax 平台的多模态生成 API（视频/图像/语音/音乐）。
所有模态共用同一个 API key，遵循统一的异步任务模式：
  POST 创建任务 → GET 轮询状态 → GET 获取文件 URL

使用方式：
    from tools.minimax_multimodal import MiniMaxMultimodal
    mm = MiniMaxMultimodal()   # 自动从 keychain 读取 minimax_apikey
    
    # 视频生成
    result = mm.generate_video(prompt="一只猫在草地上奔跑")
    
    # 图像生成
    result = mm.generate_image(prompt="星空下的城市")
    
    # 语音合成（同步，短文本）
    result = mm.synthesize_speech(text="你好世界", voice_id="Calm_Woman")
    
    # 音乐生成
    result = mm.generate_music(prompt="欢快的电子舞曲", lyrics="")

依赖：requests（GA 标准库内）
"""

import sys
import os
import time
import json
import logging
from typing import Optional, Dict, Any, List, Union
from pathlib import Path

# 确保 memory 模块可导入（已在 PATH，但以防万一）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'memory'))

try:
    import requests
except ImportError:
    raise ImportError("requests 未安装，请执行: pip install requests")

logger = logging.getLogger(__name__)

# ─── 默认配置 ───────────────────────────────────────────────────────────────
_DEFAULT_BASE_URL = "https://api.minimaxi.com/v1"
_DEFAULT_POLL_INTERVAL = 5      # 秒
_DEFAULT_MAX_POLL_TIME = 600    # 秒（10分钟）
_DEFAULT_TIMEOUT = 30           # HTTP 请求超时秒数

# 模型映射
MODELS = {
    'video': 'video-01',
    'video_live': 'video-01-live',
    'image': 'image-01',
    'image_live': 'image-01-live',
    'music': 'music-2.6',
}


class MiniMaxMultimodalError(Exception):
    """MiniMax 多模态生成专用异常"""
    pass


class TaskTimeoutError(MiniMaxMultimodalError):
    """任务轮询超时"""
    pass


class TaskFailedError(MiniMaxMultimodalError):
    """任务执行失败"""
    pass


class MiniMaxMultimodal:
    """
    MiniMax 多模态生成客户端。
    
    初始化时自动从 GA keychain 读取 minimax_apikey。
    可选传入 apibase/poll_interval/max_poll_time 覆盖默认值。
    """

    def __init__(
        self,
        apikey: Optional[str] = None,
        apibase: str = _DEFAULT_BASE_URL,
        poll_interval: int = _DEFAULT_POLL_INTERVAL,
        max_poll_time: int = _DEFAULT_MAX_POLL_TIME,
        timeout: int = _DEFAULT_TIMEOUT,
    ):
        self.apibase = apibase.rstrip('/')
        self.poll_interval = poll_interval
        self.max_poll_time = max_poll_time
        self.timeout = timeout

        # API key：优先用传入值，否则从 keychain 读取
        if apikey:
            self.apikey = apikey
        else:
            try:
                from keychain import keys
                self.apikey = keys.minimax_apikey.use()
                logger.info("MiniMax API key 已从 keychain 加载")
            except Exception as e:
                raise MiniMaxMultimodalError(
                    f"无法从 keychain 读取 minimax_apikey: {e}\n"
                    f"请先执行: from memory.keychain import keys; "
                    f"keys.set('minimax_apikey', 'sk-cp-...')"
                )

        self._headers = {
            'Authorization': f'Bearer {self.apikey}',
            'Content-Type': 'application/json',
        }

    # ═══════════════════════════════════════════════════════════════════════
    #  内部方法：任务创建 / 轮询 / 文件获取
    # ═══════════════════════════════════════════════════════════════════════

    def _create_task(self, endpoint: str, payload: dict) -> str:
        """创建异步任务，返回 task_id"""
        url = f"{self.apibase}/{endpoint}"
        logger.debug(f"POST {url} | payload keys: {list(payload.keys())}")

        resp = requests.post(url, json=payload, headers=self._headers, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()

        base_resp = data.get('base_resp', {})
        if base_resp.get('status_code', 0) != 0:
            raise MiniMaxMultimodalError(
                f"创建任务失败: {base_resp.get('status_msg', 'unknown error')} "
                f"(code={base_resp.get('status_code')})"
            )

        task_id = data.get('task_id')
        if not task_id:
            raise MiniMaxMultimodalError(f"响应中无 task_id: {data}")

        logger.info(f"任务已创建: task_id={task_id}")
        return task_id

    def _poll_task(self, endpoint: str, task_id: str, result_key: str = 'file_id') -> dict:
        """
        轮询任务状态，返回完整结果 dict。
        
        状态值：Preparing → Queueing → Generating → Success / Failed
        """
        url = f"{self.apibase}/{endpoint}"
        params = {'task_id': task_id}
        start_time = time.time()

        while True:
            elapsed = time.time() - start_time
            if elapsed > self.max_poll_time:
                raise TaskTimeoutError(
                    f"任务 {task_id} 超时（已等待 {elapsed:.0f}s > {self.max_poll_time}s）"
                )

            time.sleep(self.poll_interval)

            resp = requests.get(url, params=params, headers=self._headers, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()

            status = data.get('status', '').lower()
            logger.debug(f"任务 {task_id} 状态: {status} ({elapsed:.0f}s)")

            if status == 'success':
                logger.info(f"任务 {task_id} 成功完成 ({elapsed:.0f}s)")
                return data
            elif status in ('failed', 'error'):
                base_resp = data.get('base_resp', {})
                raise TaskFailedError(
                    f"任务 {task_id} 失败: "
                    f"{data.get('status', base_resp.get('status_msg', 'unknown'))}"
                )
            # Preparing / Queueing / Generating → 继续轮询

    def _get_file_url(self, file_id: str) -> str:
        """通过 file_id 获取文件下载 URL"""
        url = f"{self.apibase}/files/retrieve"
        params = {'file_id': file_id}
        logger.debug(f"获取文件 URL: file_id={file_id}")

        resp = requests.get(url, params=params, headers=self._headers, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()

        file_url = data.get('file', {}).get('download_url')
        if not file_url:
            raise MiniMaxMultimodalError(f"无法获取文件 URL: {data}")

        logger.info(f"文件 URL: {file_url[:80]}...")
        return file_url

    def _upload_file(self, file_path: str, purpose: str = 'retrieval') -> str:
        """上传本地文件到 MiniMax，返回 file_id"""
        url = f"{self.apibase}/files/upload"
        file_path = os.path.abspath(file_path)
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        headers = {'Authorization': f'Bearer {self.apikey}'}
        # multipart 上传，不设 Content-Type（requests 自动处理 boundary）
        with open(file_path, 'rb') as f:
            files = {'file': (os.path.basename(file_path), f)}
            data = {'purpose': purpose}
            resp = requests.post(url, files=files, data=data, headers=headers, timeout=self.timeout)

        resp.raise_for_status()
        result = resp.json()

        base_resp = result.get('base_resp', {})
        if base_resp.get('status_code', 0) != 0:
            raise MiniMaxMultimodalError(
                f"上传失败: {base_resp.get('status_msg', 'unknown')} "
                f"(code={base_resp.get('status_code')})"
            )

        file_id = result.get('file', {}).get('file_id')
        if not file_id:
            raise MiniMaxMultimodalError(f"上传响应中无 file_id: {result}")

        logger.info(f"文件已上传: file_id={file_id}")
        return file_id

    # ═══════════════════════════════════════════════════════════════════════
    #  视频生成
    # ═══════════════════════════════════════════════════════════════════════

    def generate_video(
        self,
        prompt: str,
        model: str = 'video-01',
        first_frame_image: Optional[str] = None,
        duration: int = 6,
        resolution: str = '720p',
        aspect_ratio: str = '16:9',
        prompt_optimizer: bool = True,
    ) -> dict:
        """
        生成视频（文生视频 / 图生视频）。
        
        参数：
            prompt: 视频描述（中文或英文）
            model: 模型名（video-01 / video-01-live）
            first_frame_image: 首帧图片 file_id（图生视频时提供）
            duration: 时长秒数（5/6/10）
            resolution: 分辨率（720p / 1080p）
            aspect_ratio: 宽高比（16:9 / 9:16 / 1:1 等）
            prompt_optimizer: 是否自动优化 prompt
        
        返回：
            {'task_id': '...', 'video_url': '...', 'video_width': ..., 'video_height': ..., ...}
        """
        payload = {
            'model': model,
            'prompt': prompt,
            'duration': duration,
            'resolution': resolution,
            'aspect_ratio': aspect_ratio,
            'prompt_optimizer': prompt_optimizer,
        }
        if first_frame_image:
            payload['first_frame_image'] = first_frame_image

        task_id = self._create_task('video_generation', payload)
        result = self._poll_task('query/video_generation', task_id)

        # 提取视频 URL
        video_url = result.get('video_url')
        if not video_url:
            file_id = result.get('file_id')
            if file_id:
                video_url = self._get_file_url(file_id)

        return {
            'task_id': task_id,
            'video_url': video_url,
            'video_width': result.get('video_width'),
            'video_height': result.get('video_height'),
            'status': result.get('status'),
            'raw': result,
        }

    # ═══════════════════════════════════════════════════════════════════════
    #  图像生成
    # ═══════════════════════════════════════════════════════════════════════

    def generate_image(
        self,
        prompt: str,
        model: str = 'image-01',
        subject_reference: Optional[str] = None,
        width: int = 1024,
        height: int = 1024,
        num_images: int = 1,
        prompt_optimizer: bool = True,
        style: Optional[str] = None,
    ) -> dict:
        """
        生成图像（文生图 / 图生图）。
        
        参数：
            prompt: 图像描述
            model: 模型名（image-01 / image-01-live）
            subject_reference: 主体参考图片 file_id（图生图时提供）
            width: 图片宽度像素（512-2048，需为8的倍数）
            height: 图片高度像素（512-2048，需为8的倍数）
            num_images: 生成图片数量（1-4）
            prompt_optimizer: 是否自动优化 prompt
            style: 画风设置（仅 image-01-live 支持）
        
        返回：
            {'task_id': '...', 'images': [{'url': '...', 'width': ..., 'height': ...}, ...], ...}
        """
        payload = {
            'model': model,
            'prompt': prompt,
            'width': width,
            'height': height,
            'num_images': num_images,
            'prompt_optimizer': prompt_optimizer,
        }
        if subject_reference:
            payload['subject_reference'] = subject_reference
        if style and model == 'image-01-live':
            payload['style'] = style

        # 图片生成 API 是同步的，直接返回结果（无需 task_id 轮询）
        url = f"{self.apibase}/image_generation"
        logger.debug(f"POST {url} | payload keys: {list(payload.keys())}")

        resp = requests.post(url, json=payload, headers=self._headers, timeout=self.timeout)
        resp.raise_for_status()
        result = resp.json()

        base_resp = result.get('base_resp', {})
        if base_resp.get('status_code', 0) != 0:
            raise MiniMaxMultimodalError(
                f"图片生成失败: {base_resp.get('status_msg', 'unknown error')} "
                f"(code={base_resp.get('status_code')})"
            )

        # 提取图片 URL（两种格式兼容：image_urls / image_list）
        images = []
        data = result.get('data', {})
        if 'image_urls' in data:
            # 新格式：直接返回 URL 列表
            for url_str in data['image_urls']:
                images.append({'url': url_str})
        elif 'image_list' in data:
            # 旧格式：返回含 file_id 的列表
            for img in data['image_list']:
                img_url = img.get('url')
                if not img_url and img.get('file_id'):
                    img_url = self._get_file_url(img['file_id'])
                images.append({
                    'url': img_url,
                    'width': img.get('width'),
                    'height': img.get('height'),
                })

        return {
            'task_id': result.get('id'),
            'images': images,
            'image_url': images[0]['url'] if images else None,
            'status': 'success',
            'raw': result,
        }

    # ═══════════════════════════════════════════════════════════════════════
    #  语音合成（T2A）
    # ═══════════════════════════════════════════════════════════════════════

    def synthesize_speech(
        self,
        text: str,
        voice_id: str = 'Calm_Woman',
        model: str = 'speech-01-turbo',
        speed: float = 1.0,
        vol: float = 1.0,
        pitch: int = 0,
        emotion: Optional[str] = None,
        format: str = 'mp3',
        sample_rate: int = 32000,
        channel: int = 1,
    ) -> dict:
        """
        同步语音合成（短文本，≤5000字符）。
        
        参数：
            text: 待合成文本
            voice_id: 音色ID（如 Calm_Woman, Deep_Voice_Man 等，300+音色）
            model: 模型名（speech-01-turbo / speech-01）
            speed: 语速（0.5-2.0）
            vol: 音量（0.1-10.0）
            pitch: 音调（-12到12）
            emotion: 情感（happy/sad/angry/fear/...，仅部分音色支持）
            format: 输出格式（mp3/pcm/flac）
            sample_rate: 采样率（8000/16000/24000/32000/44100）
            channel: 声道数（1=单声道 / 2=立体声）
        
        返回：
            {'audio_url': '...', 'audio_file': '/path/to/downloaded.mp3', ...}
        """
        url = f"{self.apibase}/t2a_v2"
        payload = {
            'model': model,
            'text': text,
            'voice_setting': {
                'voice_id': voice_id,
                'speed': speed,
                'vol': vol,
                'pitch': pitch,
            },
            'audio_setting': {
                'format': format,
                'sample_rate': sample_rate,
                'channel': channel,
            },
        }
        if emotion:
            payload['voice_setting']['emotion'] = emotion

        resp = requests.post(url, json=payload, headers=self._headers, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()

        base_resp = data.get('base_resp', {})
        if base_resp.get('status_code', 0) != 0:
            raise MiniMaxMultimodalError(
                f"语音合成失败: {base_resp.get('status_msg', 'unknown')} "
                f"(code={base_resp.get('status_code')})"
            )

        # 同步接口直接返回音频数据（hex 编码或 file_id）
        audio_hex = data.get('audio_file', {}).get('audio')
        file_id = data.get('audio_file', {}).get('file_id')

        result = {
            'voice_id': voice_id,
            'format': format,
            'status': 'success',
        }

        if audio_hex:
            # 同步返回：hex 编码的音频数据
            result['audio_hex_length'] = len(audio_hex)
            result['audio_hex'] = audio_hex[:200] + '...' if len(audio_hex) > 200 else audio_hex
            result['note'] = '音频数据为 hex 编码，需解码后写入文件'
        elif file_id:
            # 异步返回：需通过 file_id 获取
            result['audio_url'] = self._get_file_url(file_id)
            result['file_id'] = file_id

        return result

    def synthesize_speech_async(
        self,
        text: str,
        voice_id: str = 'Calm_Woman',
        model: str = 'speech-01-turbo',
        speed: float = 1.0,
        vol: float = 1.0,
        pitch: int = 0,
        emotion: Optional[str] = None,
        format: str = 'mp3',
        sample_rate: int = 32000,
        channel: int = 1,
        callback_url: Optional[str] = None,
    ) -> dict:
        """
        异步长文本语音合成（适用于超长文本，提交后轮询获取结果）。
        
        参数：同 synthesize_speech，额外支持：
            callback_url: 任务完成后的回调 URL（可选）
        
        返回：
            {'task_id': '...', 'audio_url': '...', ...}
        """
        url = f"{self.apibase}/t2a_async"
        payload = {
            'model': model,
            'text': text,
            'voice_setting': {
                'voice_id': voice_id,
                'speed': speed,
                'vol': vol,
                'pitch': pitch,
            },
            'audio_setting': {
                'format': format,
                'sample_rate': sample_rate,
                'channel': channel,
            },
        }
        if emotion:
            payload['voice_setting']['emotion'] = emotion
        if callback_url:
            payload['callback_url'] = callback_url

        task_id = self._create_task('t2a_async', payload)
        result = self._poll_task('query/t2a_async', task_id)

        file_id = result.get('file_id')
        audio_url = None
        if file_id:
            audio_url = self._get_file_url(file_id)

        return {
            'task_id': task_id,
            'audio_url': audio_url,
            'file_id': file_id,
            'status': result.get('status'),
            'raw': result,
        }

    # ═══════════════════════════════════════════════════════════════════════
    #  音乐生成
    # ═══════════════════════════════════════════════════════════════════════

    def generate_music(
        self,
        prompt: str,
        model: str = 'music-2.6',
        lyrics: Optional[str] = None,
        instrumental: bool = False,
    ) -> dict:
        """
        生成音乐（描述+歌词 → 人声歌曲）。
        
        参数：
            prompt: 音乐描述/灵感（如"欢快的电子舞曲"）
            model: 模型名（music-2.6）
            lyrics: 歌词（可选；留空则自动生成）
            instrumental: 是否纯音乐（无人声）
        
        返回：
            {'task_id': '...', 'audio_url': '...', 'audio_duration': ..., ...}
        """
        url = f"{self.apibase}/music_generation"
        payload = {
            'model': model,
            'refer_prompt': prompt,
        }
        if lyrics:
            payload['refer_lyrics'] = lyrics
        if instrumental:
            payload['refer_instrumental'] = instrumental

        task_id = self._create_task('music_generation', payload)
        result = self._poll_task('query/music_generation', task_id)

        file_id = result.get('file_id')
        audio_url = None
        if file_id:
            audio_url = self._get_file_url(file_id)

        return {
            'task_id': task_id,
            'audio_url': audio_url,
            'audio_duration': result.get('audio_duration'),
            'status': result.get('status'),
            'raw': result,
        }

    # ═══════════════════════════════════════════════════════════════════════
    #  文件管理
    # ═══════════════════════════════════════════════════════════════════════

    def upload_file(self, file_path: str, purpose: str = 'retrieval') -> str:
        """
        上传本地文件到 MiniMax 平台。
        
        参数：
            file_path: 本地文件路径
            purpose: 用途（retrieval/assistants 等）
        
        返回：
            file_id（用于后续引用）
        """
        return self._upload_file(file_path, purpose)

    def get_file_url(self, file_id: str) -> str:
        """
        通过 file_id 获取文件下载 URL。
        
        参数：
            file_id: 文件ID
        
        返回：
            下载 URL
        """
        return self._get_file_url(file_id)

    # ═══════════════════════════════════════════════════════════════════════
    #  便捷方法：下载文件到本地
    # ═══════════════════════════════════════════════════════════════════════

    def download_file(self, file_id: str, save_path: str) -> str:
        """
        下载文件到本地路径。
        
        参数：
            file_id: 文件ID
            save_path: 保存路径（目录或完整文件名）
        
        返回：
            实际保存的文件路径
        """
        url = self._get_file_url(file_id)
        resp = requests.get(url, timeout=self.timeout)
        resp.raise_for_status()

        if os.path.isdir(save_path):
            # 从 URL 推断文件名
            from urllib.parse import urlparse
            filename = os.path.basename(urlparse(url).path) or f"minimax_{file_id}"
            save_path = os.path.join(save_path, filename)

        os.makedirs(os.path.dirname(save_path) or '.', exist_ok=True)
        with open(save_path, 'wb') as f:
            f.write(resp.content)

        logger.info(f"文件已下载: {save_path} ({len(resp.content)} bytes)")
        return save_path


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI 入口：直接运行可测试
# ═══════════════════════════════════════════════════════════════════════════════

def _cli():
    """命令行测试入口"""
    import argparse
    parser = argparse.ArgumentParser(description='MiniMax 多模态生成工具')
    sub = parser.add_subparsers(dest='cmd')

    # 视频
    vp = sub.add_parser('video', help='生成视频')
    vp.add_argument('prompt', help='视频描述')
    vp.add_argument('--model', default='video-01')
    vp.add_argument('--duration', type=int, default=6)
    vp.add_argument('--resolution', default='720p')
    vp.add_argument('--aspect-ratio', default='16:9')
    vp.add_argument('--first-frame', help='首帧图片 file_id')

    # 图像
    ip = sub.add_parser('image', help='生成图像')
    ip.add_argument('prompt', help='图像描述')
    ip.add_argument('--model', default='image-01')
    ip.add_argument('--width', type=int, default=1024)
    ip.add_argument('--height', type=int, default=1024)
    ip.add_argument('--num', type=int, default=1)
    ip.add_argument('--subject-ref', help='主体参考图片 file_id')

    # 语音
    sp = sub.add_parser('speech', help='语音合成')
    sp.add_argument('text', help='待合成文本')
    sp.add_argument('--voice', default='Calm_Woman', help='音色ID')
    sp.add_argument('--model', default='speech-01-turbo')
    sp.add_argument('--speed', type=float, default=1.0)
    sp.add_argument('--format', default='mp3')
    sp.add_argument('--async', action='store_true', dest='use_async', help='异步模式')

    # 音乐
    mp = sub.add_parser('music', help='生成音乐')
    mp.add_argument('prompt', help='音乐描述')
    mp.add_argument('--lyrics', default='', help='歌词')
    mp.add_argument('--instrumental', action='store_true', help='纯音乐')

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    mm = MiniMaxMultimodal()

    if args.cmd == 'video':
        result = mm.generate_video(
            prompt=args.prompt,
            model=args.model,
            duration=args.duration,
            resolution=args.resolution,
            aspect_ratio=args.aspect_ratio,
            first_frame_image=args.first_frame,
        )
    elif args.cmd == 'image':
        result = mm.generate_image(
            prompt=args.prompt,
            model=args.model,
            width=args.width,
            height=args.height,
            num_images=args.num,
            subject_reference=args.subject_ref,
        )
    elif args.cmd == 'speech':
        if args.use_async:
            result = mm.synthesize_speech_async(
                text=args.text,
                voice_id=args.voice,
                model=args.model,
                speed=args.speed,
                format=args.format,
            )
        else:
            result = mm.synthesize_speech(
                text=args.text,
                voice_id=args.voice,
                model=args.model,
                speed=args.speed,
                format=args.format,
            )
    elif args.cmd == 'music':
        result = mm.generate_music(
            prompt=args.prompt,
            lyrics=args.lyrics if args.lyrics else None,
            instrumental=args.instrumental,
        )
    else:
        return

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    _cli()
