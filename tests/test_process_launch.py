from __future__ import annotations

import unittest
from unittest import mock

from bestseller_monitor import process_launch


class NoConsoleCreationFlagsTests(unittest.TestCase):
    def test_windows_uses_create_no_window(self):
        with mock.patch.object(process_launch.os, "name", "nt"), \
             mock.patch.object(process_launch.subprocess, "CREATE_NO_WINDOW",
                               0x08000000, create=True):
            self.assertEqual(process_launch.no_console_creationflags(), 0x08000000)

    def test_non_windows_uses_no_flags(self):
        with mock.patch.object(process_launch.os, "name", "posix"):
            self.assertEqual(process_launch.no_console_creationflags(), 0)
