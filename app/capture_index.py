"""抓取索引分页（POST /v1/capture-index）核心。

接收 Base64 编码的自包含 WARC，按（目标 URL 原值、WARC-Date 的 UTC 时刻、
归档序号）升序分页列出 response/revisit 抓取：

- 输入归档完整经过与 /v1/replay 相同的解码（2 MiB 上限、gzip 嗅探）与
  结构校验（截断、缺/重复记录 ID、response HTTP 头、revisit 引用链等）——
  归档中任何一处错误都使整个请求返回 422，与是否被列出无关；
- 缺 ``WARC-Target-URI`` 或 ``WARC-Date`` 的记录**跳过**（不连累整包）；
  两者俱在但日期非法（非 RFC3339）→ 422 ``invalid_capture_date``；
- 排序键为（URL 原值、UTC 时刻、归档序号）：URL 按原值字典序、不做规范化；
  ``WARC-Date`` 统一换算到 UTC 比较，同一 URL 同一时刻（含 ``±HH:MM``
  换算后相同的时刻）的记录**全部保留**，按归档序号升序；
- 每项给出记录在**解压后归档**中的字节范围 ``[offset, offset+length)``：
  自 WARC 版本行起、含完整记录块、不含记录间分隔符（CRLF CRLF），
  切片即原记录字节；
- ``cursor`` 为不透明字符串（Base64URL 编码的 JSON），绑定解压后归档的
  SHA-256 与上页末条的排序键；跨包使用、内容损坏或排序键与归档不一致
  → 422 ``invalid_cursor``；末页 ``next_cursor`` 为 ``null``。

WARC 解码与结构校验完全复用 :mod:`app.replay`；RFC3339 解析复用
:mod:`app.navigate`，与 /v1/navigate 对同一归档的判定一致。
"""

from __future__ import annotations

import base64
import bisect
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .navigate import _parse_rfc3339
from .replay import (
    ReplayError,
    _RawRecord,
    _validate_chain,
    decode_warc_payload,
    parse_warc,
    validate_records,
)

# cursor 格式版本：结构变更时递增，旧版本 cursor 一律判无效
_CURSOR_VERSION = 1


@dataclass
class _Entry:
    """一条参与列出的抓取；排序键为 (url, moment, order)。"""

    url: str  # WARC-Target-URI 原值
    moment: datetime  # WARC-Date 换算到 UTC 的时刻（感知时区）
    order: int  # 归档序号（含被跳过类型的真实序号）
    record_id: str
    record_type: str  # response / revisit
    capture_time: str  # WARC-Date 原值
    http_status: Optional[int]
    body_source_id: str
    offset: int  # 解压后归档中的起始偏移（自 WARC 版本行起）
    length: int  # 记录字节长度（含记录块、不含记录间分隔符）


def _parse_capture_date(raw: _RawRecord) -> datetime:
    """解析记录的 WARC-Date 为 UTC 时刻；非法抛 invalid_capture_date（422）。"""
    try:
        return _parse_rfc3339(raw.date or "")
    except ReplayError:
        raise ReplayError(
            "invalid_capture_date",
            f"记录 {raw.record_id_raw} 的 WARC-Date 不是合法的 RFC3339 "
            f"UTC 时间：{raw.date!r}",
            record_id=raw.record_id_raw,
            record_order=raw.order,
            warc_date=raw.date,
        )


def _encode_cursor(archive_sha: str, entry: _Entry) -> str:
    """把上页末条的排序键与归档摘要编码为不透明 cursor。"""
    payload = {
        "v": _CURSOR_VERSION,
        "sha": archive_sha,
        "url": entry.url,
        "ts": entry.moment.isoformat().replace("+00:00", "Z"),
        "order": entry.order,
    }
    raw = json.dumps(payload, ensure_ascii=False,
                     separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, archive_sha: str,
                   keys: list[tuple[str, datetime, int]]) -> int:
    """校验 cursor 并返回续取起始下标；无效情形抛 invalid_cursor（422）。

    cursor 绑定解压后归档的 SHA-256 与上页末条的排序键：摘要不符（跨包
    使用）或排序键在归档中不存在（内容损坏、归档已变化）都判无效。
    """

    def _invalid(reason: str, message: str) -> ReplayError:
        return ReplayError("invalid_cursor", message, reason=reason)

    try:
        # UnicodeEncodeError / UnicodeDecodeError / binascii.Error /
        # json.JSONDecodeError 均为 ValueError 的子类
        compact = cursor.encode("ascii", "strict")
        padded = compact + b"=" * (-len(compact) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8", "strict"))
    except ValueError:
        raise _invalid("malformed", "cursor 不是合法的 Base64URL 编码 JSON")

    if not isinstance(payload, dict) \
            or set(payload) != {"v", "sha", "url", "ts", "order"}:
        raise _invalid("malformed", "cursor 的内容结构非法")
    if payload["v"] != _CURSOR_VERSION:
        raise _invalid("unsupported_version",
                       f"cursor 版本不受支持：{payload['v']!r}")

    sha = payload["sha"]
    url = payload["url"]
    ts = payload["ts"]
    order = payload["order"]
    if not isinstance(sha, str) or not isinstance(url, str) \
            or not isinstance(ts, str) \
            or not isinstance(order, int) or isinstance(order, bool):
        raise _invalid("malformed", "cursor 的内容结构非法")
    if sha != archive_sha:
        raise _invalid(
            "archive_mismatch",
            "cursor 不属于该归档（解压后归档的 SHA-256 不匹配），不能跨包使用",
        )
    try:
        moment = _parse_rfc3339(ts)
    except ReplayError:
        raise _invalid("malformed",
                       "cursor 中的排序时刻不是合法的 RFC3339 时间")

    key = (url, moment, order)
    start = bisect.bisect_right(keys, key)
    if start == 0 or keys[start - 1] != key:
        raise _invalid(
            "stale_cursor",
            "cursor 指向的排序键在归档中不存在（归档内容可能已变化）",
        )
    return start


def capture_index(warc_b64: str, page_size: int,
                  cursor: Optional[str] = None) -> dict:
    """capture-index 入口：返回 total / entries / next_cursor。

    ``page_size`` 的 1～100 约束由请求模型校验；``cursor`` 缺省从首页开始。
    错误抛 :class:`ReplayError`（由上层映射为 HTTP 422）。
    """
    data = decode_warc_payload(warc_b64)
    archive_sha = hashlib.sha256(data).hexdigest()
    # 缺 WARC-Target-URI 的记录只是不参与列出，不应让整包判畸形；
    # capture_spans 记录每条记录在解压后归档中的字节范围
    raws = parse_warc(data, allow_missing_target_uri=True, capture_spans=True)
    by_id = validate_records(raws)

    entries: list[_Entry] = []
    for raw in raws:
        url = raw.target_uri
        date = raw.date
        if not url or not url.strip() or not date or not date.strip():
            continue  # 缺 URL 或日期的记录跳过（不影响其余记录）
        moment = _parse_capture_date(raw)
        if raw.rec_type == "revisit":
            # 链有效性已由 validate_records 保证，沿链必达终点 response
            _, endpoint = _validate_chain(raw, by_id)
            body_source_id = endpoint.record_id_raw
        else:
            body_source_id = raw.record_id_raw
        assert raw.span is not None  # capture_spans=True 时必然已填充
        offset, length = raw.span
        entries.append(
            _Entry(
                url=url,
                moment=moment,
                order=raw.order,
                record_id=raw.record_id_raw,
                record_type=raw.rec_type,
                capture_time=date,
                http_status=raw.http_status,
                body_source_id=body_source_id,
                offset=offset,
                length=length,
            )
        )

    entries.sort(key=lambda e: (e.url, e.moment, e.order))

    start = 0
    if cursor is not None:
        keys = [(e.url, e.moment, e.order) for e in entries]
        start = _decode_cursor(cursor, archive_sha, keys)

    page = entries[start:start + page_size]
    next_cursor: Optional[str] = None
    if start + page_size < len(entries):
        next_cursor = _encode_cursor(archive_sha, page[-1])

    return {
        "total": len(entries),
        "entries": [
            {
                "record_id": e.record_id,
                "record_type": e.record_type,
                "capture_time": e.capture_time,
                "http_status": e.http_status,
                "body_source_id": e.body_source_id,
                "offset": e.offset,
                "length": e.length,
            }
            for e in page
        ],
        "next_cursor": next_cursor,
    }
