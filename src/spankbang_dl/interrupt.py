"""
中断处理：让 Ctrl+C 真的能停下来。

为什么不能只靠 `except KeyboardInterrupt`
----------------------------------------
下载是在**工作线程**里跑的（`asyncio.to_thread`）。Ctrl+C 只会在主线程抛出
KeyboardInterrupt，协程那边是走任务取消（CancelledError）的路子；两条路不一定
能及时汇合，结果就是「按了没反应」。

所以这里用一个**进程级的 threading.Event** 作为唯一的中断信号：

* 信号处理器一收到 Ctrl+C 就立刻 `set()` 它 —— 下载线程在每个数据块之间检查
  这个事件，所以能在一瞬间收手（未下完的部分留在 `.part` 里，下次可续传）。
* 同时照常抛 KeyboardInterrupt，让 asyncio 正常收尾。
* 再按一次 Ctrl+C 直接 `os._exit()` 硬退出，避免任何清理逻辑卡住。
"""

from __future__ import annotations

import os
import signal
import sys
import threading

from .console import warn

_stop = threading.Event()
_press_count = 0


def stop_event() -> threading.Event:
    """全局中断信号。下载线程在每个数据块之间检查它。"""
    return _stop


def stop_requested() -> bool:
    return _stop.is_set()


def reset() -> None:
    """清空中断状态（主要给测试用）。"""
    global _press_count
    _press_count = 0
    _stop.clear()


def _flush() -> None:
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass


def install() -> None:
    """
    安装 SIGINT 处理器。必须在主线程调用。

    第一次 Ctrl+C：置位停止信号 + 抛 KeyboardInterrupt，走正常收尾。
    第二次 Ctrl+C：直接硬退出（有些清理动作——例如关 Chrome——可能卡住）。
    """

    def handler(signum, frame):
        global _press_count
        _press_count += 1

        if _press_count == 1:
            _stop.set()
            print()
            warn("收到中断，正在停止…（再按一次 Ctrl+C 可强制退出）")
            _flush()
            raise KeyboardInterrupt

        print()
        warn("再次收到中断，强制退出")
        _flush()
        os._exit(130)

    try:
        signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):
        # 不在主线程或平台不支持时忽略
        pass
