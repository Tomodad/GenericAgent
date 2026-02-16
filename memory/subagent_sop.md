# Subagent 调用 SOP

## 何时调用（调用原则）

唯一适用场景：**map模式**——将N个独立同构子任务分发给各自的subagent处理。

- 核心优势：独立上下文。避免处理文档A的长上下文污染处理文档B的质量
- 文件系统共享是优点：不同agent处理不同输入文件，产生不同输出文件
- 共享资源冲突：键鼠/浏览器主体不可共享（浏览器可分tab但需谨慎），subagent任务应限于文件处理
- 不满足map模式的任务 → 主agent顺序执行即可，别用subagent

**标准流程（map-reduce）**：
1. 主agent准备阶段：爬取/dump数据，存为多个独立输入文件
2. 分发：对每个文件启动一个subagent处理（主agent自己也可以处理其中一个）
3. 收集：等所有subagent完成，主agent读取各输出文件，汇总结果

## Task Mode 文件IO协议
- 目录：`./{task_name}/`，启动：`python agentmain.py --task {task_name} [--llm_no N]`（cwd=GenericAgent根）
- 流程：写 input.txt → 启动 → 轮询 output.txt → 读回复 → 写 reply.txt 继续 → 不写则5min自动退出
- output.txt 每轮覆盖写，用 mtime/size 判断新轮次

## 后台调用要点
```python
proc = subprocess.Popen(
    [sys.executable, 'agentmain.py', '--task', task_name],
    cwd=agent_root, creationflags=0x08000000)
```
- 必须 Popen，禁止 subprocess.run（会阻塞）
- `--llm_no` 默认=sonnet 4.5，`--llm_no 1`=opus 4.6
- 文件统一 UTF-8，subagent 无 reply 5min 自动退出无需清理