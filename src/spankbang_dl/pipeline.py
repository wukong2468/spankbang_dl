"""
主流程编排。

身份模型
--------
**唯一标识就是 stream id**，例如 `15964832`。

它来自列表页条目上的 `data-id`（实测与直链里的 `_tid` 一致），所以
**列表阶段就能判断要不要下载** —— 已经有的连视频页都不打开、直链也不解析。

身份存在文件自己身上（MP4 内嵌标签，见 `metadata.py`），不依赖文件名、不依赖清单：
改文件名、上传者改标题都不影响识别；删掉文件 = 身份一起没了 = 下次自动重下。

流水线
------
    ┌──────────────────┐   ┌──────────────┐   ┌────────────────┐   ┌──────────────┐
    │ 并行采集多个列表   │──▶│ 按 stream id │──▶│ 命中的直接跳过  │──▶│ M 个下载线程  │
    │ （N 个浏览器实例） │   │ 去重         │   │ 其余交给解析池  │   │ 下载+写元数据 │
    └──────────────────┘   └──────────────┘   └────────────────┘   └──────────────┘
                                                      ▲
                                            扫描本地库（读内嵌 id）得到「已有什么」
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import interrupt, metadata
from . import manifest as manifest_mod
from .browser import ensure_writable_temp, open_browser
from .console import C, fail, hr, human_duration, info, note, ok, progress_bar, step, warn
from .downloader import download_http_file
from .naming import (
    FilenameAllocator,
    canonical_url,
    sanitize_filename,
    stream_id_of,
)
from .scraper import collect_list, resolve_video

logger = logging.getLogger(__name__)


async def _settle(coro, timeout: float, what: str) -> None:
    """
    等一个收尾动作完成，但绝不因此卡住退出。

    任务处于「正在取消」状态时，普通 await 会立刻抛 CancelledError；关浏览器
    之类的动作也可能长时间不返回。两种情况都只记一条日志，然后继续往下走。
    """
    try:
        await asyncio.wait_for(asyncio.shield(coro), timeout=timeout)
    except asyncio.CancelledError:
        logger.debug("收尾动作（%s）被取消", what)
    except TimeoutError:
        logger.warning("收尾动作（%s）超时 %.0f 秒，跳过", what, timeout)
    except Exception as exc:
        logger.debug("收尾动作（%s）出错：%s", what, exc)


@dataclass
class RunState:
    """跨协程共享的运行状态。"""

    failed: list = field(default_factory=list)
    queued: int = 0
    skipped: int = 0            # 列表阶段就命中，连视频页都没开
    downloaded: int = 0
    download_failed: int = 0
    resolved: int = 0
    started_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active: dict = field(default_factory=dict)     # 正在下载的：id -> (已下, 总量)


class ProgressReporter:
    """把正在下载的任务汇总成一行行进度，避免多个线程互相打断输出。"""

    def __init__(self, state: RunState, interval: float = 4.0):
        self.state = state
        self.interval = interval
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
                return
            except TimeoutError:
                pass
            if not self.state.active:
                continue
            for ident, (done, total) in list(self.state.active.items()):
                print(f"    {C['dim']}[下载] {ident[:34]:34s} "
                      f"{progress_bar(done, total)}{C['reset']}")

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task


def _short(url: str) -> str:
    """给列表网址取个短标签，用于并行输出时区分是哪个列表。"""
    body = url.split("//")[-1].rstrip("/")
    for marker in ("/playlist/", "/s/"):
        if marker in body:
            head, tail = body.split(marker, 1)
            return f"{head.split('/')[0][:6]}/{marker.strip('/')}/{tail[:16]}"
    return body[:28]


# ------------------------------------------------------------------------------
# 第 1 阶段：并行采集多个列表
# ------------------------------------------------------------------------------

async def _collect_worker(index: int, headless: bool, profile_root: Path,
                          url_q: asyncio.Queue, collected: list, state: RunState,
                          args) -> None:
    """一个浏览器实例：负责采集队列里分给它的列表网址。"""
    profile = profile_root / f"c{index}"
    try:
        async with open_browser(profile, headless) as page:
            while True:
                if interrupt.stop_requested():
                    return
                try:
                    url = await asyncio.wait_for(url_q.get(), timeout=1.0)
                except TimeoutError:
                    continue
                if url is None:
                    url_q.task_done()
                    return
                tag = _short(url)
                try:
                    items = await collect_list(
                        page, url,
                        on_page=lambda p, n, a, pi, t=tag: print(
                            f"  {C['blue']}[{t}]{C['reset']} 第 {p} 页  "
                            f"{n:>3} 条（新增 {a:>3}）"
                            f"{'   ' + pi if pi else ''}"))
                    async with state.lock:
                        collected.extend(items)
                    ok(f"  [{tag}] 采集完成，{len(items)} 条")
                except Exception as exc:
                    logger.exception("采集 %s 失败", url)
                    fail(f"  [{tag}] 采集失败：{type(exc).__name__}: {str(exc)[:100]}")
                finally:
                    url_q.task_done()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("采集进程 %d 异常退出", index)
        fail(f"采集进程 {index} 异常退出：{type(exc).__name__}: {exc}")


async def _collect_all(urls: list[str], headless: bool, profile_root: Path,
                       state: RunState, args) -> list[dict]:
    """并行采集所有列表网址，返回汇总后的条目。"""
    count = max(1, min(len(urls), args.resolvers))
    info("采集进程", f"{count} 个 Chrome 进程"
                     f"{'（无头）' if headless else '（有头，会弹出窗口）'}")

    url_q: asyncio.Queue = asyncio.Queue()
    for url in urls:
        url_q.put_nowait(url)
    for _ in range(count):
        url_q.put_nowait(None)

    collected: list[dict] = []
    workers = [asyncio.create_task(
        _collect_worker(i + 1, headless, profile_root, url_q, collected, state, args))
        for i in range(count)]

    try:
        await asyncio.gather(*workers, return_exceptions=True)
    except (asyncio.CancelledError, KeyboardInterrupt):
        for task in workers:
            task.cancel()
        raise
    return collected


# ------------------------------------------------------------------------------
# 第 2 阶段：解析身份并下载
# ------------------------------------------------------------------------------

async def _resolve_worker(index: int, headless: bool, profile_root: Path,
                          resolve_q: asyncio.Queue, download_q: asyncio.Queue,
                          state: RunState, library: dict, allocator: FilenameAllocator,
                          args) -> None:
    """一个 Chrome 进程：不断从解析队列取条目，解析出直链后丢进下载队列。"""
    profile = profile_root / str(index)
    try:
        async with open_browser(profile, headless) as page:
            while True:
                if interrupt.stop_requested():
                    return
                try:
                    item = await asyncio.wait_for(resolve_q.get(), timeout=1.0)
                except TimeoutError:
                    continue
                if item is None:
                    resolve_q.task_done()
                    return
                try:
                    await _handle_item(item, page, download_q, state, library,
                                       allocator, args)
                finally:
                    resolve_q.task_done()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("解析进程 %d 异常退出", index)
        fail(f"解析进程 {index} 异常退出：{type(exc).__name__}: {exc}")
        while not resolve_q.empty():
            try:
                item = resolve_q.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is not None:
                await resolve_q.put(item)
            resolve_q.task_done()


async def _handle_item(item: dict, page, download_q: asyncio.Queue, state: RunState,
                       library: dict, allocator: FilenameAllocator, args) -> None:
    """打开视频页 → 解析直链 → 取 id → 决定 跳过 / 下载。"""
    href = item["href"]
    listing_title = item.get("title", "")
    title = sanitize_filename(listing_title) or "video"
    listed_id = item.get("stream_id")

    if not href:
        # 只有 data-id 没有链接的条目没法打开，只能记一条
        async with state.lock:
            state.failed.append({
                "stream_id": listed_id, "列表标题": listing_title,
                "error": "条目没有可打开的链接",
            })
        return

    result = await resolve_video(page, href, prefer=args.prefer)
    result["href"] = href
    result["列表标题"] = listing_title
    result["标题"] = title
    result["来源列表"] = item.get("来源列表", "")

    if result.get("error"):
        async with state.lock:
            state.failed.append(result)
        print(f"  {C['red']}× 解析失败{C['reset']}  {result['error']}  {title[:34]}")
        note(href)
        return

    url = result["url"]
    # id 优先用直链里抠出来的（权威），列表给的 data-id 作兜底
    stream_id = stream_id_of(url) or listed_id
    if not stream_id:
        result["error"] = f"拿不到 stream id：{url[:80]}"
        async with state.lock:
            state.failed.append(result)
        print(f"  {C['red']}× 无法识别 id{C['reset']}  {title[:40]}")
        return

    canonical = canonical_url(url)

    async with state.lock:
        state.resolved += 1
        # 别的地方可能刚好下完了同一个（跨列表重复），这里再确认一次
        if stream_id in library:
            state.skipped += 1
            print(f"  {C['yellow']}↷ 已有{C['reset']}  {stream_id:<10}  {title[:38]}")
            return
        filename = allocator.allocate(title, stream_id)
        library[stream_id] = {
            "标题": title,
            "文件名": filename,
            "来源": canonical,
            "来源列表": item.get("来源列表", ""),
            "时长": result.get("duration") or 0,
            "文件大小": 0,
            "下载状态": "待下载",
        }

    part = os.path.join(str(args.outdir), metadata.part_name(title, stream_id))

    video = dict(result)
    video.update({
        "stream_id": stream_id,
        "canonical": canonical,
        "文件名": filename,
        "文件路径": os.path.join(str(args.outdir), filename),
        "断点路径": part,
    })
    await download_q.put(video)


# ------------------------------------------------------------------------------
# 第 3 阶段：下载
# ------------------------------------------------------------------------------

async def _download_worker(index: int, download_q: asyncio.Queue, state: RunState,
                           library: dict, args, stop: threading.Event) -> None:
    while True:
        if interrupt.stop_requested():
            return
        try:
            video = await asyncio.wait_for(download_q.get(), timeout=1.0)
        except TimeoutError:
            continue
        if video is None:
            download_q.task_done()
            return
        try:
            await _do_download(index, video, state, library, args, stop)
        finally:
            download_q.task_done()


async def _do_download(index: int, video: dict, state: RunState, library: dict,
                       args, stop: threading.Event) -> None:
    dest = video["文件路径"]
    part = video["断点路径"]
    filename = video["文件名"]
    stream_id = video["stream_id"]

    # ---- 试运行 / 只列直链 ----
    if args.dry_run or args.list_only:
        async with state.lock:
            library[stream_id] = {
                "标题": video["标题"],
                "文件名": filename,
                "来源": video["canonical"],
                "来源列表": video.get("来源列表", ""),
                "时长": video.get("duration") or 0,
                "文件大小": 0,
                "下载状态": "试运行" if args.dry_run else "仅列直链",
                "元数据写入": "",
            }
        marker = "试运行" if args.dry_run else "直链"
        print(f"  {C['green']}√ {marker}{C['reset']}  {stream_id:<10} "
              f"{human_duration(video.get('duration') or 0):>7}  {video['标题'][:34]}")
        note(video["canonical"][:112])
        return

    head = 0
    if os.path.isfile(part):
        try:
            head = os.path.getsize(part)
        except OSError:
            head = 0
    resume = f"  断点 {head / 1048576:.1f} MB" if head else ""
    print(f"  {C['green']}→ 下载 {index}{C['reset']}  {stream_id:<10} "
          f"{human_duration(video.get('duration') or 0):>7}  "
          f"{video['标题'][:34]}{resume}")

    t0 = time.time()

    def on_progress(done, total):
        state.active[stream_id] = (done, total)

    try:
        good, error, written = await asyncio.to_thread(
            download_http_file, video["url"], dest, video.get("page_url") or "",
            stop, on_progress, 5, part)
    except Exception as exc:
        good, error, written = False, f"{type(exc).__name__}: {exc}", 0

    state.active.pop(stream_id, None)
    elapsed = max(0.001, time.time() - t0)
    speed = written / elapsed / 1048576

    if not good:
        async with state.lock:
            state.download_failed += 1
            state.failed.append({
                "stream_id": stream_id, "列表标题": video.get("列表标题", ""),
                "文件名": filename, "下载错误": error,
            })
            if stream_id in library:
                library[stream_id]["下载状态"] = "失败"
        print(f"    {C['red']}× 失败{C['reset']}  {error}  {filename[:40]}")
        return

    # ---- 成功后写内嵌身份（id + 直链）----
    ok_meta, meta_err = metadata.write_source(dest, stream_id, video["canonical"])

    try:
        size = os.path.getsize(dest)
    except OSError:
        size = written

    async with state.lock:
        state.downloaded += 1
        library[stream_id] = {
            "标题": video["标题"],
            "文件名": filename,
            "来源": video["canonical"],
            "来源列表": video.get("来源列表", ""),
            "时长": video.get("duration") or 0,
            "文件大小": size,
            "下载状态": "完成",
            "元数据写入": "完成" if ok_meta else f"失败（{meta_err}）",
        }

    print(f"    {C['green']}√ 完成{C['reset']}  {written / 1048576:>7.1f} MB  "
          f"{elapsed:>6.1f}s  {speed:>5.1f} MB/s  {filename[:34]}")
    if not ok_meta:
        warn(f"    内嵌身份写入失败：{meta_err}")


async def _manifest_writer(state: RunState, library: dict, path: str, args,
                           playlist_total: int, stop: asyncio.Event) -> None:
    """定期把清单落盘，中途 Ctrl+C 也不会丢记录。"""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=3.0)
            return
        except TimeoutError:
            pass
        async with state.lock:
            data = manifest_mod.build_manifest(
                args, playlist_total, state.queued, state, library, state.started_at)
        manifest_mod.write_manifest(path, data)


# ------------------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------------------

async def run(args) -> int:
    """完整流程。返回进程退出码。"""
    started_at = time.time()
    state = RunState()
    state.started_at = started_at
    manifest_path = str(Path(args.outdir) / args.manifest)
    Path(args.outdir).mkdir(parents=True, exist_ok=True)

    urls = [u for u in (getattr(args, "playlists", None) or [args.playlist]) if u]
    if not urls:
        fail("没有给出任何列表网址")
        return 2

    try:
        ensure_writable_temp(Path(args.profile_dir).parent,
                             prefer=Path(args.tmp_dir) if args.tmp_dir else None)
    except RuntimeError as exc:
        fail(str(exc))
        return 2

    profile_root = Path(args.profile_dir)
    headless = bool(args.headless)

    # ================================================================
    # 第 1 步：并行采集所有列表
    # ================================================================
    step(1, f"并行采集 {len(urls)} 个列表")
    for url in urls:
        note(url)
    print()
    try:
        raw = await _collect_all(urls, headless, profile_root, state, args)
    except Exception as exc:
        fail(f"采集失败：{type(exc).__name__}: {exc}")
        note("如果是「Chrome 未安装」，请确认系统装了 Google Chrome")
        return 2

    if not raw:
        fail("没有采集到任何条目，可能是被 Cloudflare 拦住了")
        note("脚本会自动等待挑战放行；若持续失败，可先手动用浏览器打开一次该页面")
        return 3
    print()
    ok(f"合计采集 {C['bold']}{len(raw)}{C['reset']} 条（跨列表未去重）")

    # ================================================================
    # 第 2 步：按 stream id 去重
    # ================================================================
    step(2, "按 stream id 跨列表去重")
    by_id: dict[str, dict] = {}
    no_id: list[dict] = []
    for entry in raw:
        sid = entry.get("stream_id")
        if not sid:
            no_id.append(entry)
            continue
        if sid not in by_id:
            by_id[sid] = entry

    dup = len(raw) - len(by_id) - len(no_id)
    info("唯一视频", f"{len(by_id)} 个")
    if dup:
        info("跨列表重复", f"{dup} 条已合并")
    if no_id:
        warn(f"{len(no_id)} 条没有 data-id，只能打开页面再判断")

    merged: list[dict] = list(by_id.values()) + no_id

    begin = max(1, args.start)
    subset = merged[begin - 1:]
    if args.limit > 0:
        subset = subset[:args.limit]
    if not subset:
        fail("按 --start / --limit 过滤后没有剩余条目")
        return 3

    # ================================================================
    # 第 3 步：扫描本地库 —— 只看文件自己内嵌的 id
    # ================================================================
    step(3, "扫描本地库（读每个视频内嵌的 stream id）")
    library = metadata.scan_library(str(args.outdir))
    allocator = FilenameAllocator(str(args.outdir))

    if library:
        ok(f"本地已有 {C['bold']}{len(library)}{C['reset']} 个视频")
        for sid, entry in sorted(library.items())[:5]:
            note(f"{sid:<10} {entry.get('文件名', '')[:52]}")
        if len(library) > 5:
            note(f"…… 其余 {len(library) - 5} 个见清单")
    else:
        note("本地还没有带 id 标签的视频")

    orphans = metadata.list_unidentified(str(args.outdir))
    if orphans:
        warn(f"有 {len(orphans)} 个视频没有 id 标签（不会被识别为已下载）")
        for name in orphans[:5]:
            note(name[:60])

    # ================================================================
    # 第 4 步：分流 —— 列表阶段就能确定要跳过的，连视频页都不开
    # ================================================================
    step(4, "分流：id 已存在的直接跳过")
    resolve_q: asyncio.Queue = asyncio.Queue()
    download_q: asyncio.Queue = asyncio.Queue()

    need = 0
    for entry in subset:
        sid = entry.get("stream_id")
        if sid and sid in library:
            state.skipped += 1
            continue
        need += 1
        resolve_q.put_nowait(entry)

    state.queued = len(subset)
    info("已存在，跳过", f"{state.skipped} 个（未开视频页、未解析直链）")
    info("需要解析并下载", f"{need} 个")

    # ================================================================
    # 第 5 步：解析身份并下载
    # ================================================================
    step(5, "解析直链并下载")
    if need:
        info("解析进程", f"{args.resolvers} 个 Chrome 进程"
                         f"{'（无头）' if headless else '（有头，会弹出窗口）'}")
    else:
        note("没有需要解析的条目，不启动浏览器")
    info("下载并发", f"{args.workers}")
    if args.list_only or args.dry_run:
        warn(("只列直链（--list-only）" if args.list_only else "试运行（--dry-run）")
             + "：不会真正下载")
    print()

    stop_thread = interrupt.stop_event()
    stop_tasks = asyncio.Event()
    reporter = ProgressReporter(state)

    # 没有任何条目需要解析时，连解析进程都不必起——它们一进函数就会开浏览器
    resolve_workers: list[asyncio.Task] = []
    if need:
        for index in range(1, args.resolvers + 1):
            resolve_workers.append(asyncio.create_task(
                _resolve_worker(index, headless, profile_root, resolve_q, download_q,
                                state, library, allocator, args)))
    download_workers: list[asyncio.Task] = []
    for index in range(1, args.workers + 1):
        download_workers.append(asyncio.create_task(
            _download_worker(index, download_q, state, library, args, stop_thread)))

    writer = asyncio.create_task(
        _manifest_writer(state, library, manifest_path, args, len(merged), stop_tasks))
    reporter.start()

    try:
        if resolve_workers:
            await resolve_q.join()
            for _ in resolve_workers:
                await resolve_q.put(None)
            await asyncio.gather(*resolve_workers, return_exceptions=True)
        await download_q.join()
        for _ in download_workers:
            await download_q.put(None)
        await asyncio.gather(*download_workers, return_exceptions=True)
    except (asyncio.CancelledError, KeyboardInterrupt):
        stop_thread.set()
        raise
    finally:
        stop_tasks.set()
        await _settle(reporter.stop(), timeout=5, what="进度显示")
        await _settle(writer, timeout=8, what="清单写入")

    # ================================================================
    # 收尾
    # ================================================================
    async with state.lock:
        data = manifest_mod.build_manifest(
            args, len(merged), state.queued, state, library, started_at)
    manifest_mod.write_manifest(manifest_path, data)

    print()
    hr("=")
    print(C["bold"] + "  汇总" + C["reset"])
    hr("=")
    print(f"  采集到条目          {len(raw)}（{len(urls)} 个列表）")
    print(f"  跨列表去重后        {len(merged)}")
    print(f"  本次处理            {len(subset)}")
    print(f"  {C['yellow']}id 已存在，跳过      {state.skipped}{C['reset']}"
          f"   （连视频页都没开）")
    if not (args.list_only or args.dry_run):
        print(f"  {C['green']}解析直链            {state.resolved}{C['reset']}")
        print(f"  {C['green']}下载完成            {state.downloaded}{C['reset']}")
        if state.download_failed:
            print(f"  {C['red']}下载失败            {state.download_failed}{C['reset']}")
    print(f"  本地视频总数        {data.get('本地视频总数', 0)}")
    print(f"  总耗时              {time.time() - started_at:.1f} 秒")
    print(f"  清单文件            {manifest_path}")
    hr("=")
    note("是否下载只看文件内嵌的 stream id；清单不参与判断。")

    if state.failed:
        print()
        warn("以下条目未成功，可稍后重跑：")
        for entry in state.failed[:10]:
            note(f"{entry.get('stream_id') or entry.get('href', '')}  -- "
                 f"{entry.get('error') or entry.get('下载错误', '')}")
        if len(state.failed) > 10:
            note(f"其余 {len(state.failed) - 10} 条见清单文件")

    print()
    return 0 if not state.failed else 1
