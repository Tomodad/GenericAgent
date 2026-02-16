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

## 4. 典型场景：CE式差集扫描定位动态字段（已验证）
用于定位微信等自绘UI中「当前会话标题」等随操作变化的内存字段。

**方法（类似Cheat Engine找游戏数值）：**
1. 找到主窗口PID（Weixin.exe有多个进程，用win32gui.GetWindowThreadProcessId取有窗口的那个）
2. 切到会话A → `scan_memory(pid, "人名A", mode="string")` → 得地址集S_A
3. 切到会话B → `scan_memory(pid, "人名B", mode="string")` → 得地址集S_B
4. 差集：S_A独有地址（A时有、B时无）= 候选地址
5. 切回A → 用ReadProcessMemory逐个读候选地址，确认内容变回"人名A"的即为目标
6. 再切第3、4个人交叉验证

**坑点：**
- 搜索切换会污染结果（搜索框缓存也含人名），最终验证应用列表点击而非搜索
- 地址是绝对虚拟地址，进程重启后失效，需重新校准（约10秒）
- ReadProcessMemory读UTF-8，用`raw.split(b'\x00')[0].decode('utf-8')`提取干净文本
- 微信主进程名为Weixin.exe（非WeChat.exe）