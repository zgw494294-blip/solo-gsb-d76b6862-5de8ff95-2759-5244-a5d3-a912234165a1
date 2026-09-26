"""多 member gzip 打包（POST /v1/member-pack）核心。

接收 Base64 编码的自包含 WARC（2 MiB 解码上限、单流/多 member gzip 均
支持），只收录其中的 ``response`` / ``revisit`` 记录，按归档原序为每条
记录生成一个**独立的 gzip member**，各 member 顺序拼接成可按记录随机定位
的多 member gzip：

- 输入归档完整经过与 /v1/replay 相同的解码（2 MiB 上限、gzip 嗅探）与
  结构/引用链校验——归档中任何一处错误（包括未收录类型的记录）都使整个
  请求失败（422）；
- 收录记录以原始字节为基础：WARC/HTTP 头与负载逐字节保留；每条记录在
  其 gzip member 中是**完整 WARC 记录 + 记录分隔符（CRLF CRLF）**，因此
  单取一个 member 解压即是一条独立可解析的 WARC 记录，整包解压后各记录
  之间也以标准分隔符相接；
- 归档**末记录缺 Content-Length**（其记录块按规则延伸至归档 EOF）时，
  补入**准确块长**：span 尾部随块读到 EOF 的记录间空行全部保留在记录
  块内，member 末尾再统一在块外追加一个 ``CRLF CRLF`` 分隔符。warcio
  解析无显式 WARC 长度的响应时，正文恰为“去掉块内最后一对空行（它与
  块外分隔符配对为记录边界）”之后的块字节——这与原归档（块延伸至
  EOF、块外无分隔符）口径一致，故 /v1/replay 还原的正文与原包逐字节
  相同，HTTP 头与负载原字节也始终保持不变；
- gzip 头使用固定时间戳（mtime=0）与固定压缩级别，同一输入永远产出
  逐字节相同的输出；
- 返回各记录 ID 对应的压缩 member 字节范围 ``[offset, offset+length)``，
  范围连续且互不重叠，可据此直接从 gzip_warc 中切出单个 member；
- 整包（解压后）超过 2 MiB → 422 ``payload_too_large``。

交付前对整包解压结果再做一次完整的回放口径校验，并与输入的
/v1/replay 结果逐条比对（状态、正文与正文来源 ID），确保打包结果交给
/v1/replay 后与原包完全一致。
"""

from __future__ import annotations

import base64
import gzip

from .merge import (
    _header_name,
    _is_blank_line,
    _line_ending,
    _record_chunk,
    _split_head,
)
from .replay import (
    MAX_DECODED_BYTES,
    ReplayError,
    _validate_chain,
    decode_warc_payload,
    gunzip_if_needed,
    parse_warc,
    validate_records,
)

# WARC 记录分隔符：CRLF CRLF（每个 gzip member 的记录块之后附带一个，
# 使单个 member 解压后即是“完整 WARC 记录及分隔符”）
_RECORD_SEPARATOR = b"\r\n\r\n"

# 固定 gzip 时间戳与压缩级别：同一输入必须产出逐字节相同的多 member gzip
_GZIP_MTIME = 0
_GZIP_LEVEL = 9


def _gzip_member(member_payload: bytes) -> bytes:
    """把“完整 WARC 记录 + 分隔符”的 member 负载压成一个独立 gzip member。

    mtime 固定为 0、不带文件名等可选头，压缩级别固定，因此输出仅取决于
    输入字节。
    """
    return gzip.compress(
        member_payload,
        compresslevel=_GZIP_LEVEL,
        mtime=_GZIP_MTIME,
    )


def _final_record_member_payload(chunk: bytes) -> bytes:
    """把缺 Content-Length 的末记录切成“记录 + 一个分隔符”的 member 负载。

    span 延伸至 EOF：其块尾可能挂着若干记录间空行，这些字节全部保留在
    记录块内（warcio 解析正文时去掉块内最后一对空行——它与 member 块外
    统一追加的分隔符配对成记录边界，与原归档“块延伸至 EOF”的正文口径
    一致）；随后注入**准确块长**（块为空即 ``Content-Length: 0``，此时
    span 止于 WARC 头结束行，:func:`app.merge._ensure_content_length`
    不会注入，故此处显式补 0），块外再统一追加一个 ``CRLF CRLF``。
    """
    head_lines, block = _split_head(chunk)
    if head_lines and _is_blank_line(head_lines[-1]) and not any(
            _header_name(line) == b"content-length"
            for line in head_lines[1:-1]):
        ending = _line_ending(head_lines[-1]) or _line_ending(
            head_lines[0]) or b"\r\n"
        chunk = (
            b"".join(head_lines[:-1])
            + b"Content-Length: " + str(len(block)).encode("ascii") + ending
            + head_lines[-1] + block
        )
    return chunk + _RECORD_SEPARATOR


def _replay_view(raw, by_id):
    """与 /v1/replay 一致的 (record_id 原值, 状态, 正文, 正文来源 ID)。"""
    if raw.rec_type == "revisit":
        _, endpoint = _validate_chain(raw, by_id)
    else:
        endpoint = raw
    return (
        raw.record_id_raw,
        raw.http_status,
        endpoint.body,
        endpoint.record_id_raw,
    )


def _validate_packed(packed: bytes, expected) -> None:
    """交付前对整包解压结果做回放口径校验并与输入逐条比对。

    结构/引用链错误，或记录 ID 顺序、HTTP 状态、正文、正文来源 ID 与输入
    不一致，一律 422 ``invalid_member_pack``，不交付伪成功的 gzip_warc。
    """
    try:
        packed_raws = parse_warc(packed)
        packed_by_id = validate_records(packed_raws)
        actual = [_replay_view(raw, packed_by_id) for raw in packed_raws]
    except ReplayError as exc:
        extra = {k: v for k, v in exc.detail.items()
                 if k not in ("error", "message")}
        raise ReplayError(
            "invalid_member_pack",
            "打包生成的 gzip WARC 未通过回放校验，拒绝交付："
            + exc.detail["message"],
            cause=exc.code,
            **extra,
        )

    if actual != expected:
        raise ReplayError(
            "invalid_member_pack",
            "打包生成的 gzip WARC 回放结果与输入不一致，拒绝交付",
            expected_count=len(expected),
            actual_count=len(actual),
        )


def member_pack(warc_b64: str) -> dict:
    """member-pack 入口：返回 gzip_warc 与各 member 的压缩字节范围。

    错误抛 :class:`ReplayError`（由上层映射为 HTTP 422）。
    """
    data = decode_warc_payload(warc_b64)
    raws = parse_warc(data, capture_spans=True)
    by_id = validate_records(raws)
    expected = [_replay_view(raw, by_id) for raw in raws]

    # 按归档原序逐记录构造 member 负载：普通记录 = span 原字节 + 分隔符；
    # 末记录缺 Content-Length（块延伸至 EOF）时补入准确块长后再追加分隔符
    member_payloads: list[bytes] = []
    for index, raw in enumerate(raws):
        chunk = _record_chunk(data, raw)
        if index == len(raws) - 1:
            member_payloads.append(_final_record_member_payload(chunk))
        else:
            member_payloads.append(chunk + _RECORD_SEPARATOR)

    packed = b"".join(member_payloads)
    if len(packed) > MAX_DECODED_BYTES:
        raise ReplayError(
            "payload_too_large",
            f"打包后（解压后）WARC 体积为 {len(packed)} 字节，超过上限 "
            f"{MAX_DECODED_BYTES} 字节（2 MiB）",
            decoded_size=len(packed),
            max_bytes=MAX_DECODED_BYTES,
        )

    # 交付前先以未压缩形态做完整回放校验（状态、正文与来源 ID 与输入一致）
    _validate_packed(packed, expected)

    # 每记录一个独立 gzip member；固定 mtime/级别保证输出确定性
    members: list[bytes] = [_gzip_member(chunk) for chunk in member_payloads]

    offsets: list[dict] = []
    cursor = 0
    for raw, member in zip(raws, members):
        length = len(member)
        offsets.append({
            "record_id": raw.record_id_raw,
            "offset": cursor,
            "length": length,
        })
        cursor += length
    gzip_warc = b"".join(members)

    # 多 member 拼接的 gzip 必须整体可解压且结果恰为 packed
    if gunzip_if_needed(gzip_warc) != packed:
        raise ReplayError(
            "invalid_member_pack",
            "打包生成的多 member gzip 解压结果与预期不一致，拒绝交付",
        )

    return {
        "count": len(raws),
        "gzip_warc": base64.b64encode(gzip_warc).decode("ascii"),
        "members": offsets,
    }
