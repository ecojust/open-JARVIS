"""Explicit role entry points for source and bundled child processes."""

import sys
from pathlib import Path


def child_command(role: str, *arguments: str) -> list[str]:
    if getattr(sys, "frozen", False):
        executable = sys.executable
        if role != "desktop" and sys.platform in {"win32", "darwin"}:
            filename = (
                "open-JARVIS-worker.exe"
                if sys.platform == "win32"
                else "open-JARVIS-worker"
            )
            executable = str(Path(executable).with_name(filename))
        prefix = [executable]
    else:
        prefix = [sys.executable, "-m", "jarvis"]
    return prefix + ["--role", role, *map(str, arguments)]
