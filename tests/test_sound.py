"""警报音默认不响：只有采集入口按配置打开之后才出声。

2026-09-13 被这条咬过：一次测试路径撞进「人工介入等待」，警报在开发机上循环
响了五分钟。默认关掉之后，「测试跑出声音」这件事在结构上不可能再发生。
"""
import unittest
from unittest.mock import MagicMock, patch

from bestseller_monitor import sound


class AlarmDefaultTests(unittest.TestCase):
    def tearDown(self):
        sound.configure(False)   # 别把状态漏给别的用例

    def test_stays_silent_until_someone_configures_it(self):
        beep = MagicMock()
        with patch.dict("sys.modules", {"winsound": beep}):
            sound.play_alarm(count=1)

        beep.Beep.assert_not_called()

    def test_plays_once_configured_on(self):
        sound.configure(True)
        beep = MagicMock()
        with patch.dict("sys.modules", {"winsound": beep}):
            sound.play_alarm(count=1)

        self.assertEqual(beep.Beep.call_count, 3, "一次警报 = 高-低-高三声")

    def test_configured_off_stays_off(self):
        sound.configure(True)
        sound.configure(False)
        beep = MagicMock()
        with patch.dict("sys.modules", {"winsound": beep}):
            sound.play_alarm(count=2)

        beep.Beep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
