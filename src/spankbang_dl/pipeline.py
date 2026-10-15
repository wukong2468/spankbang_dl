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
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import done as done_mod
from . import interrupt, metadata
from . import manifest as manifest_mod
from .browser import ensure_writable_temp, open_browser
from .config import (
    SEGMENT_CHUNK_BYTES,
    SEGMENT_FAIL_LIMIT,
    SEGMENT_FLUSH_SECONDS,
    SEGMENT_MIN_BYTES,
)
from .console import (
    C,
    fail,
    hr,
    human_duration,
    human_size,
    info,
    note,
    ok,
    progress_bar,
    step,
    warn,
)
from .downloader import download_http_file, download_range, probe_range
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
    done_entries: dict = field(default_factory=dict)   # 本次下完、待写进账本的
    active_downloads: int = 0     # 当前正在下载的视频数（用来分配每条直链分几段）
    segmented: int = 0            # 本次采用分段下载的文件数
    segment_peak: int = 0         # 实际用过的最大分段数（收尾时报告）


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
            "文件状态": "未完成",
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

def _plan_segments(args, state: RunState) -> int:
    """
    决定这条直链切几段。

    并发总数由 `--workers` 封顶，所以段数要按「同时在下几个视频」均分：

        只剩 1 个视频、--workers 50、--segments 32  ->  32 段（吃满带宽）
        剩 4 个视频                              ->  12 段
        剩 100 个视频                            ->   1 段（等于以前的单连接）

    这样视频多的时候行为完全不变，视频少了才自动提速。
    """
    max_segments = max(1, int(getattr(args, "segments", 1)))
    active = max(1, state.active_downloads)
    return max(1, min(max_segments, max(1, args.workers // active)))


async def _download_segmented(url: str, dest: str, part: str, meta_path: str,
                              total: int, max_segments: int, referer: str,
                              stop: threading.Event, state: RunState,
                              sem: asyncio.Semaphore, target_fn,
                              on_total) -> tuple[bool, str, int]:
    """
    分块并发下载一条直链（并发数**动态伸缩**）。

    为什么不是「一开始切成 N 段就不动了」
    ------------------------------------
    早先的实现是按开始那一刻的情况定死段数。但如果一开始有 100 个视频在下载，
    每个只能分到 1 段；等小视频都下完了、带宽空出来了，剩下那几个大视频
    仍然只有 1 段，白白闲着。所以改成：

        把文件按 `SEGMENT_CHUNK_BYTES` 切成很多小块
        一个监管协程每 2 秒重算一次「这条直链现在该占几条连接」
        在跑的块任务不够就再起几个，够了就不起

    这样并发数会跟着实际负载自动涨落。

    落盘
    ----
    `part` 是数据文件（预先撑到 total 大小），每块用独立句柄 seek 到自己的偏移处写。
    `meta_path` 记录哪些块已经下完，用于续传。

    `sem` 是全局连接信号量——每块在真正发请求前先抢一个槽位，
    保证所有直链的并发连接总数不超过 `--workers`。

    返回 (是否成功, 错误说明, 已写入字节数)。
    """
    chunk = SEGMENT_CHUNK_BYTES
    n_chunks = (total + chunk - 1) // chunk

    # ---- 读续传信息（对不上就重来）----
    done = set()
    try:
        with open(meta_path, encoding="utf-8") as handle:
            saved = json.load(handle)
        if (isinstance(saved, dict) and saved.get("total") == total
                and saved.get("chunk") == chunk):
            done = {int(i) for i in (saved.get("done") or []) if 0 <= int(i) < n_chunks}
    except (OSError, ValueError, TypeError):
        done = set()

    # 数据文件撑到完整大小；大小对不上说明是旧文件，续传信息作废
    try:
        size = os.path.getsize(part) if os.path.isfile(part) else 0
    except OSError:
        size = 0
    with open(part, "r+b" if size else "w+b") as handle:
        if size != total:
            handle.truncate(total)
            done = set()

    pending = [i for i in range(n_chunks) if i not in done]
    lock = threading.Lock()
    written = [len(done) * chunk]
    abort = threading.Event()
    failures = [0]
    last_save = [0.0]

    def save_meta(force: bool = False):
        now = time.monotonic()
        if not force and now - last_save[0] < 1.0:
            return
        last_save[0] = now
        with lock:
            snapshot = {"total": total, "chunk": chunk, "done": sorted(done)}
        tmp = meta_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle)
            os.replace(tmp, meta_path)
        except OSError:
            pass

    save_meta(force=True)

    async def download_chunk(index: int) -> bool:
        start = index * chunk
        end = min(total - 1, start + chunk - 1)
        while not abort.is_set() and not (stop is not None and stop.is_set()):
            # 本次尝试写进来的字节数。重试时要把它从总进度里扣掉，
            # 否则失败的那几次会被重复计入，报出来的大小会比文件还大。
            attempt = [0]

            def bump(n, _box=attempt):
                with lock:
                    _box[0] += n
                    written[0] += n

            await sem.acquire()
            try:
                good, err, _n = await asyncio.to_thread(
                    download_range, url, part, start, end, referer, stop, bump)
            finally:
                sem.release()

            if good:
                with lock:
                    done.add(index)
                failures[0] = 0
                save_meta()
                return True

            with lock:
                written[0] -= attempt[0]     # 这次没成，进度回退

            if err == "已中断" or (stop is not None and stop.is_set()):
                return False
            failures[0] += 1
            if failures[0] >= SEGMENT_FAIL_LIMIT:
                # 实测有些 CDN 对某些偏移直接回 404/416，分段没法用 —— 整体退回单连接
                abort.set()
                return False
        return False

    inflight: dict[int, asyncio.Task] = {}
    peak = [0]
    try:
        while True:
            if abort.is_set() or (stop is not None and stop.is_set()):
                break

            # ---- 重算目标并发数，把不够的补上 ----
            try:
                target = max(1, min(max_segments, int(target_fn())))
            except Exception:
                target = max(1, max_segments)
            while pending and len(inflight) < target:
                index = pending.pop()
                inflight[index] = asyncio.create_task(download_chunk(index))
            peak[0] = max(peak[0], len(inflight))

            if not inflight:
                break            # 没有待下的块，也没有在跑的 -> 收工

            finished, _ = await asyncio.wait(
                list(inflight.values()), timeout=SEGMENT_FLUSH_SECONDS,
                return_when=asyncio.FIRST_COMPLETED)
            for index in [i for i, task in inflight.items() if task.done()]:
                inflight.pop(index, None)

            with lock:
                state.segment_peak = max(state.segment_peak, len(inflight))
                on_total(written[0], total)
            save_meta()

        if abort.is_set():
            return False, "分段不被服务器接受，回退单连接", written[0]

        # 收尾：等在跑的块结束（中断时也要等，好把进度记下来）
        if inflight:
            await asyncio.gather(*inflight.values(), return_exceptions=True)

        if stop is not None and stop.is_set():
            save_meta(force=True)
            return False, "已中断（进度已保存，可续传）", written[0]

        if len(done) < n_chunks:
            save_meta(force=True)
            return False, f"还有 {n_chunks - len(done)} 块没下完", written[0]

        try:
            os.remove(meta_path)
        except OSError:
            pass
        try:
            os.replace(part, dest)
        except OSError as exc:
            return False, f"改名失败：{exc}", written[0]
        return True, "", written[0]
    finally:
        for task in inflight.values():
            task.cancel()


async def _download_worker(index: int, download_q: asyncio.Queue, state: RunState,
                           library: dict, args, stop: threading.Event,
                           sem: asyncio.Semaphore) -> None:
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
            await _do_download(index, video, state, library, args, stop, sem)
        finally:
            download_q.task_done()


async def _do_download(index: int, video: dict, state: RunState, library: dict,
                       args, stop: threading.Event, sem: asyncio.Semaphore) -> None:
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
                "文件状态": "未完成",
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

    # ---- 决定这条直链要不要分段 ----
    segments = _plan_segments(args, state)
    meta_path = part + ".json"
    total_size = 0
    use_segments = segments > 1 and not args.no_segment

    if use_segments:
        total_size, probe_err = await asyncio.to_thread(
            probe_range, video["url"], video.get("page_url") or "")
        if total_size <= 0:
            use_segments = False
            if probe_err:
                logger.debug("不分段（%s）：%s", stream_id, probe_err)
        elif total_size < max(SEGMENT_MIN_BYTES, getattr(args, "segment_min", 0)):
            use_segments = False

    if use_segments:
        # 每块至少 1 MB，免得小文件被切成一堆碎片
        from .config import CHUNK_SIZE

        segments = max(1, min(segments, max(1, total_size // CHUNK_SIZE)))
        if segments <= 1:
            use_segments = False

    plan = f"  {C['magenta']}分段并发（最多 {segments}）{C['reset']}" if use_segments else ""
    resume = f"  断点 {head / 1048576:.1f} MB" if head else ""
    print(f"  {C['green']}→ 下载 {index}{C['reset']}  {stream_id:<10} "
          f"{human_duration(video.get('duration') or 0):>7}  "
          f"{video['标题'][:34]}{plan}{resume}")

    t0 = time.time()

    def on_progress(done, total):
        state.active[stream_id] = (done, total)

    def target_segments() -> int:
        """这条直链此刻该占几条连接 —— 监管协程每 2 秒问一次，所以会动态涨落。"""
        return _plan_segments(args, state)

    async with state.lock:
        state.active_downloads += 1
    try:
        if use_segments:
            good, error, written = await _download_segmented(
                video["url"], dest, part, meta_path, total_size, segments,
                video.get("page_url") or "", stop, state, sem, target_segments,
                on_progress)
            async with state.lock:
                if good:
                    state.segmented += 1
            if not good and error.startswith("分段不被服务器接受"):
                warn(f"    {error}，改为单连接重下")
                for path in (part, meta_path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                good, error, written = await asyncio.to_thread(
                    download_http_file, video["url"], dest,
                    video.get("page_url") or "", stop, on_progress, 5, part)
        else:
            good, error, written = await asyncio.to_thread(
                download_http_file, video["url"], dest, video.get("page_url") or "",
                stop, on_progress, 5, part)
    except Exception as exc:
        good, error, written = False, f"{type(exc).__name__}: {exc}", 0
    finally:
        async with state.lock:
            state.active_downloads = max(0, state.active_downloads - 1)

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
                library[stream_id]["文件状态"] = "未完成"
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
        entry = {
            "标题": video["标题"],
            "文件名": filename,
            "来源": video["canonical"],
            "来源列表": video.get("来源列表", ""),
            "时长": video.get("duration") or 0,
            "文件大小": size,
            "下载状态": "完成",
            "元数据写入": "完成" if ok_meta else f"失败（{meta_err}）",
            "文件状态": "在磁盘上",
        }
        library[stream_id] = entry
        # 记进账本：以后即使文件被移走/删掉，也不会再重下
        state.done_entries[stream_id] = entry

    print(f"    {C['green']}√ 完成{C['reset']}  {human_size(written):>11}  "
          f"{elapsed:>6.1f}s  {speed:>5.1f} MB/s  {filename[:34]}")
    if not ok_meta:
        warn(f"    内嵌身份写入失败：{meta_err}")


async def _manifest_writer(state: RunState, library: dict, path: str, args,
                           playlist_total: int, stop: asyncio.Event,
                           done_file: str) -> None:
    """定期把清单和账本落盘，中途 Ctrl+C 也不会丢记录。"""
    done_count = 0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=3.0)
            return
        except TimeoutError:
            pass
        async with state.lock:
            data = manifest_mod.build_manifest(
                args, playlist_total, state.queued, state, library, state.started_at,
                done_count)
            pending = dict(state.done_entries)
        manifest_mod.write_manifest(path, data)
        if pending and not (args.dry_run or args.list_only):
            good, _err, done_count = done_mod.merge_done(done_file, pending)


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
    # 第 3 步：收集「已经有什么」
    #   ① 递归扫描保存目录，读每个视频内嵌的 stream id
    #   ② 已下载完成.json ——只增不删的账本，补上「文件已经不在了」的情况
    # ================================================================
    step(3, "扫描本地库 + 读取已下载完成.json")
    on_disk = metadata.scan_library(str(args.outdir))
    library: dict = dict(on_disk)

    allocator = FilenameAllocator(str(args.outdir))

    if on_disk:
        ok(f"磁盘上 {C['bold']}{len(on_disk)}{C['reset']} 个视频（已递归扫子目录）")
        for sid in sorted(on_disk)[:5]:
            note(f"{sid:<10} {on_disk[sid].get('文件名', '')[:52]}")
        if len(on_disk) > 5:
            note(f"…… 其余 {len(on_disk) - 5} 个见清单")
    else:
        note("磁盘上还没有带 id 标签的视频")

    done_file = done_mod.done_path(str(args.outdir), args.done_file)
    done_records = done_mod.load_done(done_file)
    only_done = 0
    if not args.ignore_done:
        for sid, entry in done_records.items():
            if sid in library:
                continue
            only_done += 1
            library[sid] = {
                "标题": entry.get("标题", ""),
                "文件名": entry.get("文件名", ""),
                "来源": entry.get("来源", ""),
                "来自列表": entry.get("来自列表", ""),
                "下载状态": "已有",
                "文件状态": "仅记录",
            }

    if done_records:
        ok(f"账本 {C['bold']}{len(done_records)}{C['reset']} 条"
           f"（其中 {only_done} 条磁盘上已没有文件）")
    elif args.ignore_done:
        warn("已按要求忽略账本（--ignore-done）")
    else:
        note("还没有账本文件（首次运行会新建）")

    info("判定用的 id 总数", f"{len(library)}")

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
    # 全局连接信号量：所有直链 + 所有分段的并发连接总数不超过 --workers
    sem = asyncio.Semaphore(max(1, args.workers))

    download_workers: list[asyncio.Task] = []
    for index in range(1, args.workers + 1):
        download_workers.append(asyncio.create_task(
            _download_worker(index, download_q, state, library, args, stop_thread, sem)))

    writer = asyncio.create_task(
        _manifest_writer(state, library, manifest_path, args, len(merged), stop_tasks,
                         done_file))
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
        pending = dict(state.done_entries)

    done_count = len(done_records)
    if pending and not (args.dry_run or args.list_only):
        good, err, done_count = done_mod.merge_done(done_file, pending)
        if good:
            ok(f"已下载完成.json 已更新（本次新增/更新 {len(pending)} 条，共 {done_count} 条）")
        else:
            warn(f"写入已下载完成.json 失败：{err}")

    async with state.lock:
        data = manifest_mod.build_manifest(
            args, len(merged), state.queued, state, library, started_at, done_count)
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
    print(f"  磁盘上的视频        {data.get('其中：磁盘上真实存在', 0)}"
          f"（递归扫子目录，含本次新下的）")
    print(f"  仅在账本里的        {data.get('其中：仅记录在账本里', 0)}")
    print(f"  账本条目            {done_count}")
    print(f"  判定用 id 总数      {data.get('本地视频总数', 0)}")
    print(f"  总耗时              {time.time() - started_at:.1f} 秒")
    print(f"  清单文件            {manifest_path}")
    print(f"  账本文件            {done_file}")
    hr("=")
    note("是否下载只看文件内嵌的 stream id 与已下载完成.json；清单不参与判断。")
    note("想重下某个视频：删掉文件，并把已下载完成.json 里对应的那条一起删掉。")

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
