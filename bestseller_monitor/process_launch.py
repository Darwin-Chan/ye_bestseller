"""Platform-specific child-process launch options used by the exchange station."""
from __future__ import annotations

import os
import subprocess


def no_console_creationflags() -> int:
    """Return flags that keep Windows child processes out of a new console window."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
