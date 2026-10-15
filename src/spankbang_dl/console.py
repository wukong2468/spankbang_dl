"""终端输出：中文、便于人眼阅读，自动适配是否支持 ANSI 颜色。"""

from __future__ import annotations

import os
import sys

LINE_WIDTH = 84

_COLORS = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[36m",
    "magenta": "\033[35m",
}


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        try:
            import ctypes

            k = ctypes.windll.kernel32
            handle = k.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            if k.GetConsoleMode(handle, ctypes.byref(mode)):
                k.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            return False
    return True


C = dict(_COLORS) if _supports_color() else {k: "" for k in _COLORS}


def hr(char: str = "─") -> None:
    print(C["dim"] + char * LINE_WIDTH + C["reset"])


def banner(title: str, subtitle: str = "") -> None:
    print()
    print(C["bold"] + C["blue"] + "=" * LINE_WIDTH + C["reset"])
    print(C["bold"] + C["blue"] + "  " + title + C["reset"])
    if subtitle:
        print(C["dim"] + "  " + subtitle + C["reset"])
    print(C["bold"] + C["blue"] + "=" * LINE_WIDTH + C["reset"])


def step(index: int, text: str) -> None:
    print()
    print(C["bold"] + f"【第 {index} 步】{text}" + C["reset"])


def info(label: str, value: str) -> None:
    width = 18 if len(label) > 10 else 12
    print(f"  {C['dim']}{label:<{width}}{C['reset']}{value}")


def ok(text: str) -> None:
    print(f"  {C['green']}√{C['reset']} {text}")


def warn(text: str) -> None:
    print(f"  {C['yellow']}!{C['reset']} {text}")


def fail(text: str) -> None:
    print(f"  {C['red']}×{C['reset']} {text}")


def note(text: str) -> None:
    print(f"    {C['dim']}{text}{C['reset']}")


def human_duration(seconds: float) -> str:
    """秒数 -> 人能直接读的时长，例如 `3:50` / `1:02:03`。"""
    if not seconds or seconds <= 0:
        return "未知"
    total = int(seconds)
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def human_size(num_bytes: float) -> str:
    """字节数 -> 人能直接读的大小，例如 `150.76 MB`。"""
    if num_bytes is None or num_bytes < 0:
        return "未知"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def progress_bar(done: int, total: int, width: int = 22) -> str:
    if total <= 0:
        return f"{done / 1048576:>7.1f} MB"
    ratio = min(1.0, done / total)
    filled = int(width * ratio)
    bar = "=" * filled + (">" if filled < width else "") + " " * max(0, width - filled - 1)
    return f"[{bar}] {ratio * 100:>3.0f}%  {done / 1048576:>6.1f}/{total / 1048576:.1f} MB"
