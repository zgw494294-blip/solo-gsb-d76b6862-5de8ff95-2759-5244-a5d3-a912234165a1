"""记录子集导出（POST /v1/extract）核心。

给定 Base64 WARC 与 1～100 个 WARC-Record-ID，从归档中切出所请记录及
其 revisit 引用链传递依赖，打包为新的自包含 WARC 返回：

- 输入归档完整经过与 /v1/replay 相同的解码（2 MiB 上限、gzip 嗅探）与
  结构校验（缺/重复记录 ID、response HTTP 头、revisit 引用链等）——
  归档中任何一处错误都使整个请求失败（422），与是否被请求无关；
- 请求 ID 按与归档内 ID 相同的规则规范化（去空白、去尖括号）后匹配，
  重复 ID 去重；任一 ID 在归档的 response/revisit 记录中不存在 → 422；
- 导出包仅含所请记录，以及从所请 revisit 沿 ``WARC-Refers-To`` 可达的
  记录（中间 revisit 与终点 response）——即使依赖位于所请记录之后
  （前向引用）也纳入；其余记录（含 warcinfo/request 等被跳过的类型）
  一律丢弃；
- 纳入记录按归档原序、以原始字节写出：WARC/HTTP 头与负载逐字节保留，
  WARC Content-Length 自然与写出的记录块一致；``included_ids`` 与写出
  顺序一致（取记录 ID 原始值）。

由于依赖链完整且记录字节未动，导出包交给 /v1/replay 后，所请记录的
HTTP 状态、还原正文与正文来源 ID 与原包完全一致。
"""

from __future__ import annotations

import base64

from .replay import (
    ReplayError,
    _RawRecord,
    decode_warc_payload,
    parse_warc,
    validate_records,
)

# WARC 记录分隔符：CRLF CRLF
_RECORD_SEPARATOR = b"\r\n\r\n"


def _normalize_requested_id(raw: str) -> str:
    """请求 ID 规范化：与归档内 ID 同一规则（去空白、去尖括号）。

    不做格式校验——归档内 ID 均已通过 :func:`app.replay._normalize_id`
    检查，规范后无法匹配任何记录的请求 ID 一律按 unknown_record_id 处理。
    """
    value = raw.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    value = value.strip()
    return value


def _closure(requested: list[str], by_id: dict[str, _RawRecord]) -> set[str]:
    """所请记录 + 其 revisit 引用链传递依赖（规范化 ID 集合）。

    链的有效性（每跳目标存在、无环、终点为 response）已由
    :func:`app.replay.validate_records` 对全包保证，这里沿链必达终点，
    不会失败、不会死循环。
    """
    included: set[str] = set()
    for norm in requested:
        current = by_id[norm]
        while current.record_id not in included:
            included.add(current.record_id)
            if current.rec_type != "revisit":
                break
            current = by_id[current.refers_to]  # type: ignore[index]
    return included


def extract(warc_b64: str, record_ids: list[str]) -> dict:
    """extract 入口：返回 included_ids 与导出包 Base64；错误抛 ReplayError。"""
    data = decode_warc_payload(warc_b64)
    raws = parse_warc(data, capture_spans=True)
    by_id = validate_records(raws)

    # 规范化请求 ID，按请求顺序去重；任一未知 ID 立即 422
    requested: list[str] = []
    seen: set[str] = set()
    for raw_id in record_ids:
        norm = _normalize_requested_id(raw_id)
        if norm in seen:
            continue
        seen.add(norm)
        if norm not in by_id:
            raise ReplayError(
                "unknown_record_id",
                f"记录 ID {raw_id!r} 在归档的 response/revisit 记录中不存在",
                record_id=raw_id,
            )
        requested.append(norm)

    included = _closure(requested, by_id)

    # 按归档原序切出原始字节；included_ids 与写出顺序一致
    chunks: list[bytes] = []
    included_ids: list[str] = []
    for raw in raws:
        if raw.record_id not in included:
            continue
        span = raw.span
        assert span is not None  # capture_spans=True 时必然已填充
        offset, length = span
        # 防御性剥去范围开头的空行：通过校验的归档首条记录必以 WARC/ 版本行
        # 开始（前导空行在解析阶段已判 malformed_warc），此处仅作保险
        chunks.append(data[offset:offset + length].lstrip(b"\r\n"))
        included_ids.append(raw.record_id_raw)

    # 记录间以 CRLF CRLF 分隔；末条记录之后不加分隔符——缺 Content-Length
    # 的记录块延伸至 EOF，追加任何字节都会改变其还原正文（warcio 允许末
    # 记录无尾部分隔符，见 test_complete_record_without_trailing_separator_ok）
    export = _RECORD_SEPARATOR.join(chunks)
    return {
        "included_ids": included_ids,
        "warc": base64.b64encode(export).decode("ascii"),
    }
