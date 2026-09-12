"""会话级单实例锁：同名锁第二次抢必须失败（界面单实例与采集互斥的底座）。"""
import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

from bestseller_monitor import single_instance

ROOT = Path(__file__).resolve().parent.parent


def unique_name(prefix: str = "t") -> str:
    """每个用例一把自己的锁：不跟本机真实运行的界面/采集抢同一个名字。"""
    return rf"Local\bestseller_test_{prefix}_{uuid.uuid4().hex}"


@unittest.skipUnless(os.name == "nt", "命名互斥体是 Windows 机制")
class SingleInstanceTests(unittest.TestCase):
    def test_second_acquire_of_the_same_name_fails(self):
        name = unique_name()
        first = single_instance.acquire(name)
        try:
            self.assertIsNotNone(first, "第一把锁应该抢到")
            self.assertIsNone(single_instance.acquire(name), "同一个名字不该被抢第二次")
        finally:
            first.release()

    def test_lock_is_available_again_after_release(self):
        name = unique_name()
        first = single_instance.acquire(name)
        first.release()

        second = single_instance.acquire(name)
        try:
            self.assertIsNotNone(second, "释放之后应该能再抢")
        finally:
            second.release()

    def test_is_held_asks_without_taking_the_lock(self):
        name = unique_name()
        self.assertFalse(single_instance.is_held(name), "没人持有时不该说有")

        lock = single_instance.acquire(name)
        try:
            self.assertTrue(single_instance.is_held(name))
            self.assertIsNone(
                single_instance.acquire(name), "只问不抢：探测不该把锁夺走")
        finally:
            lock.release()
        self.assertFalse(single_instance.is_held(name), "释放后不该说还持有")

    def test_lock_dies_with_the_process_that_held_it(self):
        """被强杀的进程不留下锁：暂停之后还能续跑就靠这条。"""
        name = unique_name("kill")
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys; from bestseller_monitor import single_instance; "
             f"single_instance.acquire({name!r}); print('held', flush=True); sys.stdin.read()"],
            cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            self.assertTrue(single_instance.is_held(name))
        finally:
            holder.kill()
            holder.wait(timeout=10)
            holder.stdin.close()
            holder.stdout.close()
        self.assertFalse(single_instance.is_held(name), "进程没了，锁要跟着没")


if __name__ == "__main__":
    unittest.main()
