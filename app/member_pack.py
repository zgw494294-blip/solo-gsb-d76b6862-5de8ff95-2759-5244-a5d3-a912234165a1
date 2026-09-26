"""多 member gzip 打包（POST /v1/member-pack）核心。

接收 Base64 编码的 WARC，把其中的 ``response``/``revisit`` 记录按归档原序
每条打成一个**独立的 gzip member**，拼接成可按记录随机定位的多 member
gzip：

- 输入归档完整经过与 /v1/replay 相同的解码（2 MiB 上限、gzip 嗅探）与
  结构/引用链校验（缺/重复记录 ID、response HTTP 头、revisit 引用链等）
  ——归档中任何一处错误（含不参与打包的 warcinfo/request 等记录）都使
  整个请求返回 422；
- 只收录 ``response``/``revisit``，按归档原序排列；每个 gzip member
  解压后是**一条完整 WARC 记录 + 记录分隔符（CRLF CRLF）**，可独立解压、
  独立解析；整包解压后等于这些记录以 ``CRLF CRLF`` 分隔拼接的 WARC；
- 记录以原始字节写出（WARC/HTTP 头与负载逐字节保留）；仅当输入归档的
  末记录缺 ``Content-Length``（其记录块按规则延伸至 EOF）时，为它注入
  准确块长，否则记录之后的分隔符会被当作正文的一部分；
- 每个 member 以**固定 gzip 时间戳（mtime=0）**压缩，同一输入永远产出
  相同字节；响应给出每个 member 在整包中的压缩字节范围
  ``[offset, offset+length)``，范围连续且互不重叠；
- 整包**解压后**超过 2 MiB → 422 ``payload_too_large``；交付前再把整包
  解压交给 /v1/replay 同口径校验，并比对状态、正文、来源 ID 与原包完全
  一致，否则返回 422 ``invalid_member_pack``。
"""

from __future__ import annotations

import base64
import gzip

from .merge import _ensure_content_length, _record_chunk
from .replay import (
    MAX_DECODED_BYTES,
    ReplayError,
    decode_warc_payload,
    parse_warc,
    replay,
    validate_records,
)

# WARC 记录分隔符：CRLF CRLF（每个 gzip member 解压后的结尾）
_RECORD_SEPARATOR = b"\r\n\r\n"

# 固定 gzip 成员头时间戳（epoch 0）：保证同一输入产出字节完全一致
_GZIP_MTIME = 0
_GZIP_LEVEL = 9


def _replay_signature(warc_b64: str) -> list[tuple]:
    """回放结果中与“状态、正文、来源 ID”一致相关的字段签名。

    记录 ID、类型、HTTP 状态、正文 Base64、正文来源 ID 全部一致时，打包
    前后的回放结果才视为等价（target_uri / capture_time 等原样头字段不在
    比对项中——记录字节既未改写，它们必然保持；``order`` 也不比对：打包
    丢弃 warcinfo/request 等类型后，response/revisit 在结果序列中的归档
    序号本就会相应变化，但其结果列表顺序仍与原回放一致）。
    """
    return [
        (
            rec["record_id"],
            rec["record_type"],
            rec["http_status"],
            rec["body_base64"],
            rec["body_source_id"],
        )
        for rec in replay(warc_b64)
    ]


def member_pack(warc_b64: str) -> dict:
    """member-pack 入口：返回 count / gzip_warc / members；错误抛 ReplayError。"""
    data = decode_warc_payload(warc_b64)
    raws = parse_warc(data, capture_spans=True)
    validate_records(raws)

    # 按归档原序切出记录原始字节；仅末记录可能缺 Content-Length（记录块
    # 延伸至 EOF），为它注入准确块长，保证 member 尾部的 CRLF CRLF 是
    # 分隔符而不是其正文的一部分。
    member_payloads: list[bytes] = []
    for index, raw in enumerate(raws):
        chunk = _record_chunk(data, raw)
        if index == len(raws) - 1:
            chunk = _ensure_content_length(chunk)
        member_payloads.append(chunk + _RECORD_SEPARATOR)

    # 整包解压后的字节 = 各 member 解压内容首尾相接
    packed = b"".join(member_payloads)
    if len(packed) > MAX_DECODED_BYTES:
        raise ReplayError(
            "payload_too_large",
            f"打包后（解压后）WARC 体积为 {len(packed)} 字节，超过上限 "
            f"{MAX_DECODED_BYTES} 字节（2 MiB）",
            decoded_size=len(packed),
            max_bytes=MAX_DECODED_BYTES,
        )

    # 每条记录独立压缩为一个 gzip member（固定时间戳），记录压缩范围
    out = bytearray()
    members: list[dict] = []
    for raw, payload in zip(raws, member_payloads):
        member = gzip.compress(
            payload, compresslevel=_GZIP_LEVEL, mtime=_GZIP_MTIME
        )
        offset = len(out)
        out.extend(member)
        members.append({
            "record_id": raw.record_id_raw,
            "offset": offset,
            "length": len(member),
        })

    packed_b64 = base64.b64encode(bytes(out)).decode("ascii")

    # 交付前把整包解压交给回放口径校验（结构 + 引用链），并比对打包前后的
    # 状态、正文与来源 ID 完全一致；任何不符都返回 422 而非伪成功。
    try:
        packed_raws = parse_warc(packed)
        validate_records(packed_raws)
        if _replay_signature(base64.b64encode(packed).decode("ascii")) \
                != _replay_signature(warc_b64):
            raise ReplayError(
                "invalid_member_pack",
                "打包生成的多 member gzip 解压后回放结果与原归档不一致，"
                "拒绝交付",
            )
    except ReplayError as exc:
        if exc.code == "invalid_member_pack":
            raise
        extra = {k: v for k, v in exc.detail.items()
                 if k not in ("error", "message")}
        raise ReplayError(
            "invalid_member_pack",
            "打包生成的多 member gzip 未通过回放校验，拒绝交付："
            + exc.detail["message"],
            cause=exc.code,
            **extra,
        )

    return {
        "count": len(raws),
        "gzip_warc": packed_b64,
        "members": members,
    }
