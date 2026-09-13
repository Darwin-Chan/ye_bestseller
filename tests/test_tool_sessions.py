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

# 「自己起一份」的指纹：调试端口与用户数据目录只准由 browser_pw.open_session 拼；
# 自己 taskkill 会把用户的 Edge 一起带走（按镜像名杀尤其危险），收尾该走 close_session。
FORBIDDEN = {
    "remote-debugging-port": "自己拉起浏览器调试端口（应走 browser_pw.open_session）",
    "--user-data-dir": "自己去占共享 profile 起浏览器（应走 browser_pw.open_session）",
    "taskkill": "自己杀浏览器进程（应走 browser_pw.close_session / browser_proc）",
}

# 本条改的就是这三个：它们必须真的经那一对接口开合，而不是「没命中指纹」就算过关
# （用 cfg 拼出端口再 connect_over_cdp 的写法不含指纹，却仍是自己起的一份）。
SESSION_TOOLS = (
    "diag_card_selector.py",
    "diag_list_dom.py",
    "diag_offer_id_presolve.py",
)


class ToolsShareOneBrowserSessionTests(unittest.TestCase):
    def test_no_tool_hand_rolls_a_browser_session(self):
        offenders = []
        for path in sorted(TOOLS.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for needle, why in FORBIDDEN.items():
                if needle in text:
                    offenders.append(f"{path.relative_to(TOOLS)}: {why}")
        self.assertEqual(offenders, [])

    def test_the_three_diagnostic_tools_go_through_the_shared_session(self):
        for name in SESSION_TOOLS:
            with self.subTest(tool=name):
                text = (TOOLS / name).read_text(encoding="utf-8")
                self.assertIn("browser_pw.open_session(", text)
                self.assertIn("browser_pw.close_session(", text)


if __name__ == "__main__":
    unittest.main()
