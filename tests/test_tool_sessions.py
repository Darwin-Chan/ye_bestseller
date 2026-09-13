"""诊断工具有且只有一个浏览器会话（候选 03 / ADR-0019）。

「调试端口 / 用户数据目录 / Edge 路径 / 就绪等待 / 收尾归属」这五个事实只准写在
`browser_pw.open_session` / `close_session` 一处。工具自己再起一份，就是又开一版副本：
上一版六个诊断脚本各写一遍（含 `time.sleep(9)` 与 `taskkill /IM msedge.exe /F`），
后者会把机器上所有 Edge 窗口一起关掉。

这条护栏盯的是结构，不是行为：谁再往 `tools/` 里塞一份拉起浏览器的代码，这里就红。
"""
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"

# 「自己起一份」的指纹：调试端口只准由 browser_pw.open_session 拼；
# 按镜像名杀进程会把用户的 Edge 一起带走，任何工具都不该有。
FORBIDDEN = {
    "remote-debugging-port": "自己拉起浏览器调试端口（应走 browser_pw.open_session）",
    '"taskkill", "/IM"': "按镜像名杀光所有 Edge（应走 browser_pw.close_session）",
}


class ToolsShareOneBrowserSessionTests(unittest.TestCase):
    def test_no_tool_hand_rolls_a_browser_session(self):
        offenders = []
        for path in sorted(TOOLS.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for needle, why in FORBIDDEN.items():
                if needle in text:
                    offenders.append(f"{path.name}: {why}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
