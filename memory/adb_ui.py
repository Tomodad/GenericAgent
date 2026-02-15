# adb_ui.py - 一键dump+解析Android UI
# PITFALLS: dump已内置--compressed; 美团等动画app需先禁动画(adb shell settings put global animator_duration_scale 0 ...共3条);
# 弹窗检测: ui(clickable_only=True, raw=True) 找全屏FrameLayout+底部小ImageView(关闭X)
# 已知包名: 美团外卖=com.sankuai.meituan.takeoutnew 淘宝=com.taobao.taobao
import subprocess, xml.etree.ElementTree as ET, os, re, shutil

ADB = shutil.which("adb") or "adb"
LOCAL_XML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_mt.xml")

def ui(keyword=None, clickable_only=False, raw=False):
    """一键dump+解析Android UI
    keyword: 过滤含关键词的节点
    clickable_only: 只显示可点击节点
    raw: 返回原始节点列表而非打印
    """
    subprocess.run([ADB, "shell", "rm", "-f", "/sdcard/ui.xml"], capture_output=True)
    r = subprocess.run([ADB, "shell", "uiautomator", "dump", "--compressed", "/sdcard/ui.xml"],
                       capture_output=True, text=True, timeout=15)
    if "dumped" not in r.stdout.lower() and "dumped" not in r.stderr.lower():
        print(f"dump failed: {r.stdout}{r.stderr}")
        return []
    subprocess.run([ADB, "pull", "/sdcard/ui.xml", LOCAL_XML], capture_output=True, timeout=10)

    tree = ET.parse(LOCAL_XML)
    nodes = []
    for n in tree.getroot().iter("node"):
        text = n.get("text", "")
        desc = n.get("content-desc", "")
        bounds = n.get("bounds", "")
        click = n.get("clickable") == "true"
        cls = n.get("class", "").split(".")[-1]
        label = text or desc
        if not label and not raw:
            continue
        if clickable_only and not click:
            continue
        if keyword and keyword.lower() not in (label or "").lower():
            continue
        cx, cy = 0, 0
        if bounds:
            m = re.findall(r'\[(\d+),(\d+)\]', bounds)
            if len(m) == 2:
                cx = (int(m[0][0]) + int(m[1][0])) // 2
                cy = (int(m[0][1]) + int(m[1][1])) // 2
        nodes.append({"label": label, "click": click, "bounds": bounds, "cx": cx, "cy": cy, "class": cls})

    if not raw:
        for n in nodes:
            flag = "Y" if n["click"] else " "
            coord = f"({n['cx']},{n['cy']})" if n['cx'] else ""
            print(f"[{flag}] {n['label']}  {coord}  {n['bounds']}")
        print(f"\ntotal: {len(nodes)} nodes")
    return nodes

def tap(x, y):
    subprocess.run([ADB, "shell", "input", "tap", str(x), str(y)], capture_output=True)
    print(f"tap({x},{y}) ok")

if __name__ == "__main__":
    ui()