"""会话级单实例锁：同名锁第二次抢必须失败（界面单实例与采集互斥的底座）。"""
import os
import subprocess
import sys
import time
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
        # wait() 的进程退出通知可能先于命名内核对象销毁；有限等待真实释放，
        # 不把等待超时算成功，也不改变生产侧的锁探测／新建门禁。
        deadline = time.monotonic() + 2.0
        while single_instance.is_held(name) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(single_instance.is_held(name), "进程退出后命名锁必须在截止时间内释放")


if __name__ == "__main__":
    unittest.main()
