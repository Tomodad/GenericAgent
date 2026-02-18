```markdown
# TMWebDriver 极简说明（L3）

- 位置：`../TMWebDriver.py`
- 角色：本项目的 **Web 桥接层**，支撑 `web_scan` / `web_execute_js` 等高层工具。

## 和 WebDriver / Playwright 的关键差异

- 它**不是** Selenium WebDriver 或 Playwright。
- 主要优势：
  - 不需要单独开“调试浏览器”或新用户数据目录。
  - 可以**直接接管你当前已经在用的浏览器**（含现成登录状态、Cookie 等），由 Tampermonkey 脚本转发命令和结果。
- 典型适用场景：
  - 在用户日常使用的浏览器里做轻量自动化：读 DOM、执行简单 JS、在当前页面上点按/滚动等。

## 关键限制（未来可能踩坑的点）

- 受浏览器 **InTrusted / 权限策略** 限制，有些操作不能单靠 TMWebDriver 完成：
  1. **打开新窗口 / 新标签**  
     - 已通过 GM_openInTab 替换 window.open 解决。潜在问题：部分浏览器可能仍需用户显式允许脚本打开新标签。
  2. **上传文件等受信任交互**  
     - 通常无法单纯用 JS 填充 `<input type="file">` 等敏感控件。
     - 需要配合键鼠控制工具（`ljqCtrl.py` / 控制 SOP）在前台模拟真实点击和文件选择。
     - **文件上传操作要点**：①点击前用 `SetForegroundWindow` 确保浏览器窗口最前；②用 ljqCtrl 物理点击上传按钮（禁止JS click）；③用 Win32 `FindWindow` 轮询检测文件对话框是否弹出，确认后再输入路径；④操作后同样轮询检测对话框是否关闭，再继续后续步骤。

- 结论：  
  - TMWebDriver 适合“读信息 + 普通页面操作”；  
  - 对“新窗口授权、文件上传”这类敏感操作，应默认联想到：**需要和 Ctrl 工具协同**，而不是强行在 JS 里搞定。
```
## 导航避坑
- `web_scan` 仅读当前页，不会导航。
- 切换网站用 `web_execute_js` + `location.href = 'url'`。

## Google图片搜索操作
- **class名不可靠**：Google的class均为混淆名(如F0uyec)，随版本变化，禁止硬编码
- 点击图片结果：找搜索结果区内 `[role=button]` 的div，而非外层容器或内部a/img
- `web_scan` 会过滤边栏内容，边栏弹出后用JS提取：
  - 文本：`document.body.innerText`
  - 大图：遍历所有img，按 `naturalWidth` 最大的那个取src（通常>600px）
- "访问"链接：遍历所有`a`找`textContent.includes('访问')`的href
- 缩略图base64：结果中`img[src^="data:image"]`可直接提取保存
- 下载大图时注意JS返回的src可能被截断，用`return img.src`获取完整URL

## Chrome下载PDF（fetch+blob方案）
- 场景：页面上的PDF链接点击后会在浏览器内预览而非下载
- 方案：用JS `fetch(url)` 获取blob → `URL.createObjectURL(blob)` → 创建隐藏`<a>`标签设`download`属性 → 触发click → 自动下载到~/Downloads
- 模板：
  ```js
  fetch('PDF_URL').then(r=>r.blob()).then(b=>{
    const a=document.createElement('a');
    a.href=URL.createObjectURL(b);
    a.download='filename.pdf';
    a.click();
  });
  ```
- 注意：需同源或CORS允许；跨域时可能需要先导航到目标域再执行
