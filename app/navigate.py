"""离线时点导航（POST /v1/navigate）核心。

给定 Base64 WARC、起始 HTTP(S) URL 与 RFC3339 UTC 时点，离线还原该时点
浏览器会看到的页面：

- 每个 URL 选择 ``WARC-Date`` 不晚于时点的最新 response/revisit，同一时刻
  （同一 URL、同一 WARC-Date）取归档中靠后者；
- 缺 ``WARC-Target-URI`` 或 ``WARC-Date`` 的记录不参与选择（不影响归档中
  其余记录的解析），``WARC-Date`` 非法则整个请求 422；
- 选中 301/302/303/307/308 时，从**该记录自己**的 HTTP ``Location`` 解析
  相对地址（相对当前 URL），继续按时点选取；
- 缺 ``Location``、目标非 HTTP(S)、成环、超过 8 跳 → 422；
- 某一跳无合格抓取 → 404；
- revisit 只继承链终点 response 的**正文**，不借用其状态码或 Location。

WARC 的解码、gzip、2 MiB 限制与结构校验完全复用 :mod:`app.replay`；唯一
差别是 navigate 容忍缺 ``WARC-Target-URI`` 的记录（它们只是不参与选择），
其余结构校验（截断、缺/重复记录 ID、revisit 引用链等）保持一致。

:func:`_follow_redirects` 是“时点选取 + 重定向跟随”的完整循环，
/v1/page-assets 对根页面与每个静态资源都复用它。
"""

from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urldefrag, urljoin, urlsplit

from .replay import ReplayError, ResolvedRecord, load_records

# 最多选取 8 条记录（含起点与终点）；第 9 条即超限
MAX_HOPS = 8
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# 严格 RFC 3339 date-time：YYYY-MM-DDTHH:MM:SS[.frac](Z|±HH:MM)
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:[Zz]|[+-]\d{2}:\d{2})$"
)


def _parse_rfc3339(value: str) -> datetime:
    """解析 RFC3339 时间为感知时区的 UTC datetime；非法抛 ReplayError。

    接受 ``Z``/``±HH:MM`` 偏移并统一换算到 UTC；不接受无时区的“裸”时间。
    """
    if not isinstance(value, str) or not _RFC3339_RE.match(value.strip()):
        raise ReplayError(
            "invalid_at",
            "at 必须是 RFC3339 格式的 UTC 时点（如 2021-05-05T10:00:00Z）",
            at=value,
        )
    text = value.strip()
    # datetime.fromisoformat（3.11+）接受小写 z，统一换成显式 UTC 偏移
    normalized = text[:-1] + "+00:00" if text[-1] in ("z", "Z") else text
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        raise ReplayError(
            "invalid_at",
            "at 必须是 RFC3339 格式的 UTC 时点（如 2021-05-05T10:00:00Z）",
            at=value,
        )
    return dt.astimezone(timezone.utc)


def _http_url(value: str) -> str:
    """要求是绝对的 http(s) URL，原样返回；否则抛 ReplayError。"""
    if not isinstance(value, str) or not value.strip():
        raise ReplayError(
            "invalid_start_url",
            "url 必须是绝对的 HTTP(S) 地址",
            url=value,
        )
    text = value.strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        parts = None
    if parts is None or parts.scheme.lower() not in ("http", "https") \
            or not parts.hostname:
        raise ReplayError(
            "invalid_start_url",
            "url 必须是绝对的 HTTP(S) 地址",
            url=value,
        )
    # 浏览器发起 HTTP 请求前会去掉片段（fragment），不作为 URL 身份的一部分
    return urldefrag(text)[0]


def _parse_record_date(rec: ResolvedRecord) -> datetime:
    """解析记录的 WARC-Date；缺失由调用方先排除，非法直接 422。"""
    try:
        return _parse_rfc3339(rec.date or "")
    except ReplayError:
        raise ReplayError(
            "invalid_capture_date",
            f"记录 {rec.record_id_raw} 的 WARC-Date 不是合法的 RFC3339 "
            f"UTC 时间：{rec.date!r}",
            record_id=rec.record_id_raw,
            record_order=rec.order,
            warc_date=rec.date,
        )


def _build_index(records: list[ResolvedRecord]
                 ) -> dict[str, list[tuple[datetime, int, ResolvedRecord]]]:
    """按精确 URL 建索引：跳过缺 URL/日期的记录，非法日期 422。

    每个 URL 下按 (WARC-Date, 归档序号) 升序排列；同刻取后者即取末尾。
    """
    index: dict[str, list[tuple[datetime, int, ResolvedRecord]]] = {}
    for rec in records:
        if not rec.target_uri or not rec.target_uri.strip() or not rec.date \
                or not rec.date.strip():
            continue  # 缺 URL 或日期的记录不参与选择
        dt = _parse_record_date(rec)
        index.setdefault(rec.target_uri, []).append((dt, rec.order, rec))
    for entries in index.values():
        entries.sort(key=lambda item: (item[0], item[1]))
    return index


def _select(index, url: str, at: datetime) -> Optional[ResolvedRecord]:
    """选 WARC-Date <= at 的最新记录；同刻取归档后者。"""
    candidates = index.get(url)
    if not candidates:
        return None
    chosen: Optional[ResolvedRecord] = None
    for dt, _order, rec in candidates:
        if dt <= at:
            chosen = rec
    return chosen


def _hop(rec: ResolvedRecord, url: str) -> dict:
    return {
        "url": url,
        "record_id": rec.record_id_raw,
        "capture_time": rec.date,
        "http_status": rec.http_status,
    }


def _follow_redirects(index, start_url: str, point_in_time: datetime,
                      at: str) -> tuple[list[dict], str, ResolvedRecord]:
    """从 ``start_url`` 起按时点选取并跟随重定向，直到非 3xx 终点。

    返回 ``(path, final_url, 终点记录)``。无抓取（404）与重定向异常
    （422）抛 :class:`ReplayError`，detail 与 /v1/navigate 的错误响应
    完全一致：/v1/page-assets 对根页面直接透传这些错误，对静态资源则
    逐项捕获并降级为条目上的原因标记。
    """
    url = start_url
    path: list[dict] = []
    visited: set[str] = set()

    while True:
        rec = _select(index, url, point_in_time)
        if rec is None:
            raise ReplayError(
                "capture_not_found",
                f"URL {url} 在 {at} 之前没有可用的 response/revisit 抓取",
                status_code=404,
                url=url,
                at=at,
                path=path,
            )

        if url in visited:
            raise ReplayError(
                "redirect_cycle",
                f"重定向在 URL {url} 处成环",
                url=url,
                path=path + [_hop(rec, url)],
            )
        visited.add(url)
        path.append(_hop(rec, url))

        # revisit 只继承终点正文；状态/Location 一律看它自己（可能为 None）。
        status = rec.http_status
        if status not in _REDIRECT_STATUSES:
            return path, url, rec

        location = rec.http_location
        if not location:
            raise ReplayError(
                "missing_location",
                f"记录 {rec.record_id_raw} 是 {status} 重定向但缺少 HTTP "
                "Location 头",
                url=url,
                record_id=rec.record_id_raw,
                http_status=status,
                path=path,
            )

        target = urljoin(url, location)
        target_parts = urlsplit(target)
        if target_parts.scheme.lower() not in ("http", "https") \
                or not target_parts.hostname:
            raise ReplayError(
                "invalid_redirect_target",
                f"记录 {rec.record_id_raw} 的 Location {location!r} 解析出的"
                f"目标 {target!r} 不是 HTTP(S) 地址",
                url=url,
                record_id=rec.record_id_raw,
                location=location,
                resolved_url=target,
                path=path,
            )
        target = urldefrag(target)[0]

        if len(path) >= MAX_HOPS:
            raise ReplayError(
                "too_many_redirects",
                f"重定向超过 {MAX_HOPS} 跳上限（第 {len(path) + 1} 跳指向 "
                f"{target}）",
                url=url,
                next_url=target,
                max_hops=MAX_HOPS,
                path=path,
            )

        url = target


def navigate(warc_b64: str, start_url: str, at: str) -> dict:
    """navigate 入口：返回最终页面结果；错误抛 :class:`ReplayError`。"""
    url = _http_url(start_url)
    point_in_time = _parse_rfc3339(at)

    records = load_records(warc_b64, allow_missing_target_uri=True)
    index = _build_index(records)

    path, final_url, rec = _follow_redirects(index, url, point_in_time, at)
    return {
        "path": path,
        "final_url": final_url,
        "body_base64": base64.b64encode(rec.body).decode("ascii"),
        "body_source_id": rec.body_source_id,
    }
