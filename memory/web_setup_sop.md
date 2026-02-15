# Web 工具链初始化执行 SOP

若 web_scan 和 web_execute_js 已测试可用，无需执行此 SOP。
仅供初始安装时，code_run 可用但 web 工具尚未配置的场景。

## 目标
在仅具备系统级权限（code_run）时，建立 Web 交互能力（web_scan / web_execute_js）。

## 前置：检测浏览器
```python
import shutil, subprocess
browser = "chrome" if shutil.which("chrome") else "msedge"  # Edge内置必存在，Chrome可选
```

## 阶段一：安装 Tampermonkey (手动)
**状态**: 尚未实现自动化，需用户手动操作。
1. 用 `start` 打开扩展商店页面（自动适配浏览器）：
   - Chrome: `start "" "https://chromewebstore.google.com/detail/篡改猴测试版/gcalenpjmijncebpfijmoaglllgpjagf"`
   - Edge: `start "" "https://microsoftedge.microsoft.com/addons/detail/tampermonkey/iikmkjmpaadaobahmlepeloendndfphd"`
2. 提示用户点击"安装"并确认。

## 阶段二：安装 ljq_web_driver.user.js
**脚本路径**: `../assets/ljq_web_driver.user.js`

### 方案A（自动化，优先）
本地 HTTP 服务器 + TM 中间页，用 `start` 命令打开：
1. Python 启动 `http.server` 托管脚本（Content-Type: text/javascript）
2. `start "" "https://www.tampermonkey.net/script_installation.php#url=http://127.0.0.1:{port}/ljq_web_driver.user.js"`
   - ⚠️ 以上步骤均须用 `Popen` 非阻塞执行，禁止 `subprocess.run`，否则阻塞 agent
3. TM 秒弹安装确认，用户点"安装"即可

### 方案B（手动 fallback）
若方案A失败，用剪贴板：
1. 读取脚本内容 → `pyperclip.copy()`
2. 通知用户在 TM 中【新建脚本 → 全选 → 粘贴 → 保存】

## 阶段三：验证
调用 `web_scan` 或注入 JS 心跳检测，确认脚本已生效。

## 避坑 (Chromium untrusted 拦截)
- ❌ 直接导航到 `localhost/.user.js` → Chromium 弹 untrusted 拦截 + "另存为"，延迟约1分钟
- ✅ 必须用 `start` 命令（系统级）打开 TM 中间页 URL → 秒弹安装，无拦截
- 此问题 Chrome 和 Edge 均存在（Chromium 内核通病）