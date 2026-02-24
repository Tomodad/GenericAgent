# Memory Scanner SOP

## 1. 快速开始
内存特征搜索工具，支持 Hex (CE 风格) 和 字符串匹配。特别提供 LLM 模式，方便大模型分析内存上下文。

**Python 调用方式:**
```python
import sys
sys.path.append('../memory') # 直接挂载工具目录
from mem_scanner import scan_memory

# 示例：搜索特定 Hex 特征码，开启 llm_mode 以获取上下文
results = scan_memory(pid, "48 8b ?? ?? 00", mode="hex", llm_mode=True)
```

**CLI:**
```powershell
# 基础搜索
python ../memory/mem_scanner.py <PID> "pattern" --mode string

# LLM 增强模式（输出包含上下文的 JSON，推荐）
python ../memory/mem_scanner.py <PID> "pattern" --llm
```

## 2. 典型场景：结构体或关键数据定位
1. 确定目标数据的前导特征或已知常量（如特定的 Header 或 Magic Number）。
2. 在目标进程中搜索该特征：
   `scan_memory(pid, "4D 5A 90 00", mode="hex", llm_mode=True)`
3. 分析返回的 JSON 中 `context` 字段，查看目标地址前后的原始字节及 ASCII 预览。

## 3. 注意事项
- **权限**: 并非强制要求管理员权限，但需具备对目标进程的 `PROCESS_QUERY_INFORMATION` 和 `PROCESS_VM_READ` 权限。
- **效率**: 搜索大块内存时，尽量提供更唯一的特征码以减少误报。

## 4. CE式差集扫描定位动态字段
定位微信等自绘UI中随操作变化的内存字段（如当前会话标题）。核心：一次全量scan + 多次ReadProcessMemory筛选。

**流程（3个联系人A/B/C即可收敛）：**
1. 取PID：Weixin.exe有多进程，用`win32gui.GetWindowThreadProcessId`取有窗口的
2. 当前会话=A → `scan_memory(pid, "人名A", mode="string")` → 地址集S
3. 切到B → 读S全部地址 → 保留内容≠"人名A"的 → 候选C
4. 切到A → 读C全部地址 → 保留内容=="人名A"的 → 候选C'（通常1-3个）
5. 若C'>1 → 再切B/C重复 → 直到唯一

**切换会话+读地址 完整代码：**
```python
import sys; sys.path.append('../memory')
import ljqCtrl, pygetwindow as gw, pyperclip, time, ctypes

def switch_chat(name):
    win = gw.getWindowsWithTitle('微信')[0]
    if win.isMinimized: win.restore()
    win.activate(); time.sleep(0.3)
    S = 1 / ljqCtrl.dpi_scale
    ljqCtrl.Click(int((win.left+150)*S), int((win.top+40)*S)); time.sleep(0.5)
    pyperclip.copy(name); ljqCtrl.Press('ctrl+v'); time.sleep(1.5)
    ljqCtrl.Click(int((win.left+150)*S), int((win.top+130)*S)); time.sleep(0.8)

def read_addrs(pid, addrs):
    k32 = ctypes.windll.kernel32
    hp = k32.OpenProcess(0x10, False, pid)
    buf = ctypes.create_string_buffer(256)
    rd = ctypes.c_size_t()
    result = {}
    for a in addrs:
        a = int(a, 16) if isinstance(a, str) else a
        k32.ReadProcessMemory(hp, ctypes.c_void_p(a), buf, 256, ctypes.byref(rd))
        result[a] = buf.raw.split(b'\x00')[0].decode('utf-8', errors='ignore').strip()
    k32.CloseHandle(hp)
    return result  # {addr: text}
```

**坑点：**
- 进程名Weixin.exe（非WeChat.exe）；地址字符串先`int(addr,16)`
- 步骤3筛≠A（排除空/乱码），步骤4筛==A（正向确认），交替最快
- 搜索框粘贴后≥1.5s再点结果；"文件传输助手"等常见词首条可能是广告，建议用不常见的联系人名做差集（如真实好友昵称），或搜索后点第2条结果
- 切换后用read_addrs验证确实切成功了再继续
- 最终候选>1时的消歧：不要用搜索切换，直接在联系人列表中点一个靠后的人（不经过搜索框），然后read_addrs看哪个地址变了——变的才是标题栏，没变的是搜索框残留