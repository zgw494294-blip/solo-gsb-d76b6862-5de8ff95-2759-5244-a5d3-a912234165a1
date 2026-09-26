"""快照对比（POST /v1/snapshot-diff）核心。

给定旧版、新版两份 Base64 WARC 与一个 RFC3339 时点，比较该时点两份
归档中各自可见的 URL：

- 每份归档独立经过 :func:`app.replay.load_records` 的完整结构校验与
  revisit 正文还原（2 MiB 上限、缺 ``WARC-Target-URI`` 记录容忍但不入
  快照等规则与 /v1/navigate 一致）；``WARC-Record-ID`` 重复只在同一份
  归档内判错，两份归档出现相同 ID 互不影响；
- 每份归档以 URL 建立快照：只纳入 ``WARC-Date`` 不晚于时点的
  response/revisit，取（抓取时间，归档序号）最大的一条——与
  /v1/navigate 的选取规则完全一致，直接复用其索引与选取函数；缺 URL
  或日期的记录不入快照，非法日期整个请求 422；
- 内容是否变化只看还原后的正文字节与记录自身的 HTTP 状态；记录 ID、
  抓取时间与正文来源（body_source_id）的差异都不算内容变化。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Optional

from .navigate import _build_index, _parse_rfc3339, _select
from .replay import ResolvedRecord, load_records


def _snapshot(warc_b64: str, at: datetime) -> dict[str, ResolvedRecord]:
    """单份归档的时点快照：URL -> 该时点选中的记录。"""
    records = load_records(warc_b64, allow_missing_target_uri=True)
    index = _build_index(records)
    snap: dict[str, ResolvedRecord] = {}
    for url in index:
        rec = _select(index, url, at)
        if rec is not None:
            snap[url] = rec
    return snap


def _side(rec: ResolvedRecord) -> dict:
    """一侧的选中记录信息：记录 ID、自身 HTTP 状态、正文 SHA-256。"""
    return {
        "record_id": rec.record_id_raw,
        "http_status": rec.http_status,
        "body_sha256": hashlib.sha256(rec.body).hexdigest(),
    }


def _item(url: str, old: Optional[ResolvedRecord],
          new: Optional[ResolvedRecord]) -> dict:
    return {
        "url": url,
        "old": _side(old) if old is not None else None,
        "new": _side(new) if new is not None else None,
    }


def snapshot_diff(old_warc_b64: str, new_warc_b64: str, at: str) -> dict:
    """snapshot-diff 入口：返回四类 URL 列表与汇总；错误抛 ReplayError。"""
    point_in_time = _parse_rfc3339(at)
    old_snap = _snapshot(old_warc_b64, point_in_time)
    new_snap = _snapshot(new_warc_b64, point_in_time)

    only_old: list[dict] = []
    only_new: list[dict] = []
    changed: list[dict] = []
    unchanged: list[dict] = []

    for url in sorted(set(old_snap) | set(new_snap)):
        old_rec = old_snap.get(url)
        new_rec = new_snap.get(url)
        item = _item(url, old_rec, new_rec)
        if old_rec is None:
            only_new.append(item)
        elif new_rec is None:
            only_old.append(item)
        elif old_rec.body == new_rec.body \
                and old_rec.http_status == new_rec.http_status:
            unchanged.append(item)
        else:
            changed.append(item)

    return {
        "summary": {
            "only_old": len(only_old),
            "only_new": len(only_new),
            "changed": len(changed),
            "unchanged": len(unchanged),
        },
        "only_old": only_old,
        "only_new": only_new,
        "changed": changed,
        "unchanged": unchanged,
    }
