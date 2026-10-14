"""内置下载器：HTTP 直链下载，支持 `.part` 断点续传。

设计要点
--------
* 先写 `<文件名>.part`，全部读完才 `os.replace` 成正式文件名。
  这样保存目录里**永远不会出现「下了一半却顶着正式文件名」的文件**，
  下次运行也就不会把半成品误判成已下载。
* 下次运行看到 `.part` 还在，就发 `Range: bytes=<已有字节>-` 从断点继续。
"""

from __future__ import annotations

import os
import random
import ssl
import threading
import time
import urllib.error
import urllib.request

from .config import (
    CHUNK_SIZE,
    DOWNLOAD_REFERER,
    DOWNLOAD_UA,
    PART_SUFFIX,
)
from .console import progress_bar, warn

_ssl_fallback_warned = False


class DownloadCancelled(Exception):
    """下载被中断。"""


def open_url(request: urllib.request.Request, timeout: float = 30.0):
    """
    发请求，优先做正常的 SSL 证书校验。

    万一失败（公司代理、杀软的中间人证书、系统根证书过期都可能触发），
    就降级为不校验再试一次，并且只提示一次。
    """
    global _ssl_fallback_warned
    try:
        return urllib.request.urlopen(
            request, timeout=timeout, context=ssl.create_default_context()
        )
    except urllib.error.URLError as exc:
        # urllib 会把证书错误包在 URLError.reason 里
        if not isinstance(getattr(exc, "reason", None), ssl.SSLCertVerificationError):
            raise
        if not _ssl_fallback_warned:
            _ssl_fallback_warned = True
            print()
            warn("SSL 证书校验失败，已降级为「不校验证书」继续下载")
        return urllib.request.urlopen(
            request, timeout=timeout, context=ssl._create_unverified_context()
        )


def download_http_file(
    url: str,
    dest: str,
    referer: str = DOWNLOAD_REFERER,
    stop: threading.Event | None = None,
    on_progress=None,
    retries: int = 5,
    part_path: str | None = None,
) -> tuple[bool, str, int]:
    """
    下载一条 HTTP 直链到 dest，支持断点续传。

    返回 (是否成功, 错误说明, 已写入字节数)。

    part_path
        断点文件的路径；不给就用 `dest + ".part"`。
        调用方通常传一个**按身份（id + 分辨率）命名**的路径，这样即使最终
        文件名因为磁盘状态变化而换了，中断后依然能找回同一个断点续传。

    关于重试
    --------
    实测这类 CDN 偶发 `SSL: UNEXPECTED_EOF_WHILE_READING`（TLS 握手/读取中途被
    掐断）以及「读到一半连接断掉」。这两种都是**瞬时**问题——同一条直链过一会儿
    再下就正常了。所以这里多给几次机会，并且每次都从 `.part` 断点续传，
    不会白白重下已经拿到的那部分。
    """
    part = part_path or (dest + PART_SUFFIX)
    last_error = ""

    for attempt in range(1, retries + 1):
        try:
            pos = os.path.getsize(part) if os.path.isfile(part) else 0
        except OSError:
            pos = 0

        if attempt > 1:
            # 退避 + 抖动，避免多个线程同时重试把对方挤掉
            delay = min(2.0 ** attempt, 15.0) + random.uniform(0, 1.5)
            warn(f"第 {attempt}/{retries} 次重试"
                 f"{f'（从 {pos / 1048576:.1f} MB 续传）' if pos else ''}…")
            time.sleep(delay)

        headers = {
            "User-Agent": DOWNLOAD_UA,
            "Referer": referer,
            "Accept": "*/*",
            "Accept-Encoding": "identity",   # 别让 gzip 干扰 Range
            "Connection": "keep-alive",
        }
        if pos > 0:
            headers["Range"] = f"bytes={pos}-"

        try:
            resp = open_url(urllib.request.Request(url, headers=headers), timeout=30)
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 403, 404, 416) and pos > 0:
                # 断点已失效或签名过期，从零重来
                last_error = f"HTTP {exc.code}（断点失效，改为从头下载）"
                pos = 0
                try:
                    os.remove(part)
                except OSError:
                    pass
                continue
            last_error = f"HTTP {exc.code} {exc.reason}"
            continue
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue

        with resp:
            if pos > 0 and resp.status != 206:
                pos = 0                      # 服务器忽略了 Range

            total = 0
            content_range = resp.headers.get("Content-Range")
            if content_range and "/" in content_range:
                try:
                    total = int(content_range.rsplit("/", 1)[1])
                except ValueError:
                    total = 0
            if not total:
                try:
                    total = pos + int(resp.headers.get("Content-Length") or 0)
                except ValueError:
                    total = 0

            try:
                with open(part, "ab" if pos else "wb") as handle:
                    while True:
                        if stop is not None and stop.is_set():
                            raise DownloadCancelled()
                        block = resp.read(CHUNK_SIZE)
                        if not block:
                            break
                        handle.write(block)
                        pos += len(block)
                        if on_progress:
                            on_progress(pos, total)
            except DownloadCancelled:
                return False, "已中断（进度已保存，可续传）", pos
            except Exception as exc:
                last_error = f"读取/写入失败：{type(exc).__name__}: {exc}"
                continue

        if total and pos < total:
            last_error = f"数据不完整（{pos}/{total} 字节）"
            continue

        try:
            os.replace(part, dest)
        except OSError as exc:
            # 极少数情况：另一个进程/线程还占着这个文件
            last_error = f"改名失败：{exc}"
            continue
        return True, "", pos

    return False, last_error or "下载失败", 0


__all__ = [
    "DownloadCancelled",
    "download_http_file",
    "open_url",
    "progress_bar",
]
