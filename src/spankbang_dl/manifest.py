"""
身份清单：记录本地每个视频的身份与来源。

**清单不参与「要不要下载」的判断。** 那个判断只看文件自己内嵌的身份标签
（见 `metadata.scan_library`）——这样删掉文件就等于下次重下，不会因为清单里
还写着「已下载」而漏掉。

清单的用途是「一眼看清手里都有些什么」：以 stream id 为键，把分辨率、标题、
文件名、来源直链、文件大小、下载状态都摊开。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime

from .console import warn


def build_manifest(args, playlist_total: int, queued: int, state,
                   library: dict, started_at: float) -> dict:
    """
    组装清单内容。

    library 是本次的 `{stream_id: 信息}` 表（含刚下完的）。
    """
    from .naming import resolution_of

    records: dict = {}
    for stream_id, info in sorted(library.items(), key=lambda kv: str(kv[0])):
        # library 里两种来源的形状略有不同：
        #   * 扫描出来的（metadata.scan_library）：有 source，没标题
        #   * 刚下完的（pipeline 写入）：有 标题 / 来源
        # 这里统一一下，别让已有视频在清单里缺字段。
        filename = info.get("文件名", "")
        source = info.get("来源") or info.get("source", "")
        records[str(stream_id)] = {
            # 分辨率纯粹是给人看的，从直链最后那段（-480p）反推，不参与任何判断
            "分辨率": resolution_of(source),
            "标题": info.get("标题") or os.path.splitext(filename)[0],
            "文件名": filename,
            "来源": source,
            "来自列表": info.get("来源列表", ""),
            "时长": info.get("时长", 0),
            "文件大小": info.get("文件大小", 0),
            "下载状态": info.get("下载状态", "已有"),
            "元数据写入": info.get("元数据写入", ""),
        }

    on_disk = sum(1 for i in library.values() if i.get("文件大小"))

    return {
        "说明": "以 stream id 为键的身份清单。是否下载只看文件内嵌的 id，本清单不参与判断。",
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "列表网址": list(getattr(args, "playlists", None) or [args.playlist or ""]),
        "保存目录": str(args.outdir),
        "条目总数": playlist_total,
        "本次处理": queued,
        "本地视频总数": len(records),
        "其中：本次新下载": sum(1 for i in library.values()
                                if i.get("下载状态") == "完成"),
        "其中：已有跳过": sum(1 for i in library.values()
                              if i.get("下载状态") == "已有"),
        "失败": len(state.failed),
        "写入清单时磁盘上实际存在": on_disk,
        "总耗时秒": round(time.time() - started_at, 1),
        "视频清单": records,
        "失败清单": state.failed,
    }


def write_manifest(path: str, data: dict) -> bool:
    """写清单。先写临时文件再改名，避免写一半被中断留下坏文件。"""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        warn(f"写入清单文件失败：{exc}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False
