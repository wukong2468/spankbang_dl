"""命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import (
    DEFAULT_PROFILE_DIR,
    DEFAULT_RESOLVERS,
    DEFAULT_WORKERS,
    QUALITY_ORDER,
)
from .console import C, banner, fail, info, note, warn
from .interrupt import install as install_interrupt_handler
from .pipeline import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spankbang-dl",
        description="批量采集 SpankBang 列表（播放列表 / 搜索列表），"
                    "解析最高清晰度直链并下载到本地。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  spankbang-dl --playlist https://spankbang.com/xxxx/playlist/yyy/\n"
            "  spankbang-dl --playlist \"网址1 网址2\"      # 空格分隔多个列表\n"
            "  spankbang-dl                                # 不指定则在控制台提示输入\n"
            "  spankbang-dl --resolvers 6 --workers 5      # 加大并发\n"
            "  spankbang-dl --dry-run --limit 3            # 只解析不下载\n"
            "  spankbang-dl --list-only                    # 只列直链\n"
        ),
    )
    parser.add_argument("--playlist", "--list", dest="playlist", nargs="+", default=None,
                        metavar="网址",
                        help="列表网址，可以给多个（空格分隔）；"
                             "不指定时会在控制台提示输入")
    parser.add_argument("--outdir", default=str(Path.cwd() / "Downloads"),
                        help="下载保存目录")
    parser.add_argument("--headless", action="store_true",
                        help="无头模式（本站在无头下会被 Cloudflare 拦，默认有头）")
    parser.add_argument("--resolvers", type=int, default=DEFAULT_RESOLVERS,
                        help=f"并行浏览器进程数（采集和解析都用，默认 {DEFAULT_RESOLVERS}）")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"并行下载数（默认 {DEFAULT_WORKERS}）；"
                             "真正的瓶颈通常是带宽，并不是越大越快")
    parser.add_argument("--prefer", default=None, choices=QUALITY_ORDER,
                        help="优先尝试的清晰度；缺失时自动向下回退。"
                             "只影响新视频，已下载的不会因为清晰度变化而重下")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个（0 = 不限制）")
    parser.add_argument("--start", type=int, default=1, help="从第几个条目开始（从 1 计数）")
    parser.add_argument("--dry-run", action="store_true", help="只解析不下载")
    parser.add_argument("--list-only", action="store_true", help="只列出直链，不下载")
    parser.add_argument("--profile-dir", default=str(DEFAULT_PROFILE_DIR),
                        help="浏览器 profile 存放目录（每个进程一份）")
    parser.add_argument("--tmp-dir", default=None,
                        help="Playwright 的临时目录（默认自动挑一个可写的，如 ./.tmp）")
    parser.add_argument("--manifest", default="下载清单.json", help="清单文件名")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return parser


def _normalize_playlist(raw: str) -> str | None:
    """
    把用户输入整理成一个可用的网址。

    顺手做几件小事：去掉粘贴时带的引号、补上漏掉的 https://、挡掉明显不是网址的输入。
    返回 None 表示这个输入不能用。
    """
    url = (raw or "").strip().strip('"').strip("'").strip()
    if not url:
        return None

    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")

    match = re.match(r"^https?://([^/\s?#]+)", url, re.I)
    if not match or "." not in match.group(1):
        return None

    # 播放列表页和搜索页都支持，认不出来只提醒，不挡
    if "/playlist/" not in url and "/s/" not in url:
        warn(f"地址里既没有 /playlist/ 也没有 /s/，可能不是列表页：{url[:60]}")
    return url


def _split_urls(chunks: list[str]) -> list[str]:
    """把 `--playlist "a b" --playlist c` 这种输入摊平成网址列表。"""
    urls: list[str] = []
    for chunk in chunks:
        for piece in re.split(r"[\s,]+", chunk or ""):
            url = _normalize_playlist(piece)
            if url and url not in urls:
                urls.append(url)
    return urls


def _ask_for_playlist(preset: list[str] | None) -> list[str] | None:
    """
    确定要处理的列表网址（可能有多个）。

    命令行给了 --playlist 就直接用；没给就在控制台提示用户输入：
    可以一行塞多个（空格分隔），也可以一个一个来；**直接回车结束**，输入 q 退出。
    返回 None 表示没拿到任何可用网址。
    """
    if preset:
        urls = _split_urls(preset)
        if not urls:
            fail(f"--playlist 里没有有效网址：{' '.join(preset)[:120]}")
            return None
        return urls

    # 管道 / 重定向场景下没法交互，直接报错比卡住好
    if not (sys.stdin and sys.stdin.isatty()):
        fail("没有指定列表地址，而且当前不是交互式终端")
        note('请这样指定：--playlist "https://spankbang.com/xxxxx/playlist/yyy/"')
        return None

    print()
    print(C["bold"] + "  请输入列表网址" + C["reset"])
    note("播放列表页和搜索列表页都行，例如：")
    note("    https://spankbang.com/5868a/playlist/favorites/")
    note("    https://spankbang.com/s/vam/")
    print()
    note("可一次输入多个（用空格分隔），也可以一个一个来：输一个回车一次")
    note("直接回车结束输入；输入 q 退出")

    urls: list[str] = []
    while True:
        prompt = "  > " if not urls else f"  > ({len(urls)} 个已收) "
        try:
            raw = input(C["green"] + prompt + C["reset"])
        except (EOFError, KeyboardInterrupt):
            print()
            if urls:
                break
            note("已取消")
            return None

        text = raw.strip()
        if text.lower() in {"q", "quit", "exit"}:
            note("已取消")
            return None
        if not text:
            break

        before = len(urls)
        for piece in re.split(r"[\s,]+", text):
            url = _normalize_playlist(piece)
            if url and url not in urls:
                urls.append(url)
        if len(urls) == before:
            fail("这看起来不是一个网址，请重新输入")

    if not urls:
        note("没有输入任何网址")
        return None
    print()
    note(f"共收到 {len(urls)} 个网址")
    return urls


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    playlists = _ask_for_playlist(args.playlist)
    if not playlists:
        fail("没有拿到列表地址，已退出")
        return 2
    args.playlists = playlists
    args.playlist = playlists[0]

    Path(args.outdir).mkdir(parents=True, exist_ok=True)

    banner("SpankBang 列表批量下载", "采集最高清晰度直链 -> 并行下载到本地")
    info("列表网址", f"{len(playlists)} 个")
    for url in playlists:
        note(url)
    info("保存目录", args.outdir)
    info("浏览器", "Playwright + 系统 Chrome（有头）")
    info("浏览器进程", f"{args.resolvers} 个（采集与解析共用）")
    info("下载并发", f"{args.workers}")
    if args.workers > 32:
        warn(f"下载并发 {args.workers} 偏高")
        note("真正的瓶颈通常是出口带宽：并发翻倍不会让总速度翻倍，"
             "反而会互相抢带宽，还可能被 CDN 限速。")
    info("清晰度", (f"优先 {args.prefer}，缺失则向下回退" if args.prefer
                    else "自动取最高（4k > 1080p > 720p > 480p > 320p > 240p）"))
    if args.dry_run or args.list_only:
        info("运行模式", C["yellow"]
             + ("只列直链（--list-only）" if args.list_only else "试运行（--dry-run）")
             + C["reset"])
    if not args.headless and not (args.dry_run or args.list_only):
        print()
        warn("会弹出 Chrome 窗口，可能抢走终端焦点")
        note("要中断请先点一下本终端窗口，再按 Ctrl+C（按两次可强制退出）")

    # 必须尽早装：Ctrl+C 直接置位中断信号，下载线程才能立刻收手
    install_interrupt_handler()

    return _run_pipeline(args)


# 中断后最多给收尾这么久，超时就硬退出
GRACE_SECONDS = 8.0


def _hard_exit(code: int) -> None:
    """绕开所有 Python 清理逻辑直接退出。"""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)


def _run_pipeline(args) -> int:
    """
    自己管事件循环，好让 Ctrl+C 之后的退出时间由我们说了算。

    为什么不能用 asyncio.run
    ------------------------
    1. 它退出时会 `shutdown_default_executor()`，也就是**等线程池里所有任务结束**。
       下载是跑在线程里的，如果某个线程正好卡在一次 TCP 连接或读取上
       （socket 超时最长 30 秒），进程就会一直不退出——表现就是「按了 Ctrl+C 没反应」。
    2. 它用的是默认线程池，上限 `min(32, cpu+4)`，`--workers` 再大也超不过它。

    所以这里：显式按 `--workers` 建线程池；中断后给一小段时间让协程收尾
    （关浏览器、写清单），到点还没完就 `os._exit()` 直接退出。此刻 `.part`
    和清单都已经落盘，不会丢东西。
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # 关键：asyncio.to_thread 用的是 loop 的默认 executor，不显式设置的话
    # 上限永远是 min(32, cpu+4)，--workers 给多大都白搭。
    pool_size = max(1, args.workers)
    loop.set_default_executor(
        ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix="dl")
    )

    task = loop.create_task(run(args))

    try:
        return loop.run_until_complete(task)
    except KeyboardInterrupt:
        task.cancel()
        warn("正在收尾…（再按一次 Ctrl+C 立刻强制退出）")
        deadline = time.monotonic() + GRACE_SECONDS
        while not task.done() and time.monotonic() < deadline:
            try:
                loop.run_until_complete(asyncio.sleep(0.05))
            except KeyboardInterrupt:
                break
            except BaseException:
                break
        if not task.done():
            warn(f"收尾超过 {GRACE_SECONDS:.0f} 秒，强制退出")
        _hard_exit(130)
    finally:
        # 关线程池也要有上限：卡在网络上的下载线程不能拖住退出
        try:
            loop.run_until_complete(
                asyncio.wait_for(loop.shutdown_default_executor(), timeout=5)
            )
        except BaseException:
            pass
        try:
            loop.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
