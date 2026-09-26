"""摘要核验（POST /v1/digest-audit）核心。

接收 Base64 编码的自包含 WARC，逐条核验 response/revisit 记录的
``WARC-Block-Digest`` 与 ``WARC-Payload-Digest``：

- 摘要值形如 ``<算法>:<Base32>``；支持 ``sha1``、``sha256``，Base32 为
  **无填充**形式、大小写不限（算法名同样大小写不敏感）。缺算法前缀、
  Base32 字符非法、长度与算法不符等**格式错误**，或**算法不受支持**，
  都使整个请求返回 422，并指出记录 ID 与字段名；
- **Block 摘要**对原始记录块计算：含 HTTP 头、不含 WARC 头；缺
  ``Content-Length`` 的末记录，记录块以 EOF 为界；
- **Payload 摘要**对 HTTP 正文计算；revisit 用引用链终点 response 的
  正文（与 /v1/replay 的还原口径一致）；
- 两个字段各自标为 ``absent``（未声明）/ ``match`` / ``mismatch``；
  失配不阻断后续记录，响应附失配总数（按字段计）。

WARC 解码（2 MiB 上限、gzip 嗅探）与结构校验完全复用
:mod:`app.replay`，与 /v1/replay 对同一归档的判定一致。
"""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Optional

from .replay import (
    ReplayError,
    _RawRecord,
    _validate_chain,
    decode_warc_payload,
    parse_warc,
    validate_records,
)

# 算法名（小写）-> (hashlib 算法名, 无填充 Base32 字符数)
_SUPPORTED_DIGESTS = {
    "sha1": ("sha1", 32),
    "sha256": ("sha256", 52),
}
# 无填充 Base32（RFC 4648）字符集，大小写不限；不含填充符 "="
_BASE32_RE = re.compile(r"[A-Za-z2-7]+")


def _record_block(data: bytes, span: tuple[int, int]) -> bytes:
    """从归档原始字节中切出记录块：WARC 头（含终止空行）之后、记录结束之前。

    记录结束位置即 span 终点：声明了 ``Content-Length`` 时为声明块的末尾；
    缺 ``Content-Length`` 的末记录 span 延伸至 EOF，记录块自然以 EOF 为界。
    WARC 头在解析阶段已被 warcio 接受，故版本行之后的首个空行即头部终点。
    """
    offset, length = span
    end = offset + length
    pos = offset
    while pos < end:
        nl = data.find(b"\n", pos, end)
        if nl == -1:
            break
        # 与 warcio 的头部解析一致：只含空白字符的行即头部终止行
        if not data[pos:nl].rstrip():
            return data[nl + 1:end]
        pos = nl + 1
    return b""  # 防御：通过校验的记录头部必然以空行结束


def _parse_digest(value: str, *, field: str, raw: _RawRecord) -> tuple[str, bytes]:
    """解析 ``<算法>:<Base32>`` 摘要头，返回 ``(hashlib 算法名, 期望摘要字节)``。

    格式错误抛 ``invalid_digest_format``，算法不在 sha1/sha256 抛
    ``unsupported_digest_algorithm``；两者均为 422，并指出记录 ID 与字段。
    """
    text = value.strip()
    algo, sep, digest_text = text.partition(":")
    algo = algo.strip()
    digest_text = digest_text.strip()
    if not sep or not algo or not digest_text:
        raise ReplayError(
            "invalid_digest_format",
            f"记录 {raw.record_id_raw} 的 {field} 不是 "
            f"\"<算法>:<Base32>\" 形式的摘要：{text!r}",
            record_id=raw.record_id_raw,
            record_order=raw.order,
            field=field,
            value=text,
        )

    supported = _SUPPORTED_DIGESTS.get(algo.lower())
    if supported is None:
        raise ReplayError(
            "unsupported_digest_algorithm",
            f"记录 {raw.record_id_raw} 的 {field} 使用了不支持的摘要算法 "
            f"{algo!r}（仅支持 sha1、sha256）",
            record_id=raw.record_id_raw,
            record_order=raw.order,
            field=field,
            algorithm=algo,
        )

    hash_name, b32_len = supported
    if len(digest_text) != b32_len or not _BASE32_RE.fullmatch(digest_text):
        raise ReplayError(
            "invalid_digest_format",
            f"记录 {raw.record_id_raw} 的 {field} 的 Base32 部分非法："
            f"{algo} 摘要应为 {b32_len} 个无填充 Base32 字符，"
            f"实际为 {digest_text!r}",
            record_id=raw.record_id_raw,
            record_order=raw.order,
            field=field,
            value=text,
        )
    # 字符集与长度已校验，补填充解码必然成功
    padded = digest_text.upper() + "=" * (-len(digest_text) % 8)
    return hash_name, base64.b32decode(padded)


def _check_digest(raw_value: Optional[str], computed: bytes, *, field: str,
                  raw: _RawRecord) -> str:
    """核验单个摘要字段，返回 absent / match / mismatch。"""
    if raw_value is None:
        return "absent"
    hash_name, expected = _parse_digest(raw_value, field=field, raw=raw)
    actual = hashlib.new(hash_name, computed).digest()
    return "match" if actual == expected else "mismatch"


def digest_audit(warc_b64: str) -> dict:
    """digest-audit 入口：按归档原序返回每条记录的核验结果与失配总数。"""
    data = decode_warc_payload(warc_b64)
    raws = parse_warc(data, capture_spans=True)
    by_id = validate_records(raws)

    records: list[dict] = []
    mismatch_count = 0
    for raw in raws:
        if raw.rec_type == "revisit":
            # Payload 摘要用引用链终点 response 的正文；链有效性已由
            # validate_records 保证，这里沿链必达终点。
            _, endpoint = _validate_chain(raw, by_id)
            payload = endpoint.body
        else:
            payload = raw.body

        assert raw.span is not None  # capture_spans=True 时必然已填充
        block = _record_block(data, raw.span)

        block_result = _check_digest(
            raw.block_digest_raw, block, field="WARC-Block-Digest", raw=raw
        )
        payload_result = _check_digest(
            raw.payload_digest_raw, payload,
            field="WARC-Payload-Digest", raw=raw,
        )
        mismatch_count += (block_result == "mismatch") + (
            payload_result == "mismatch"
        )
        records.append(
            {
                "order": raw.order,
                "record_id": raw.record_id_raw,
                "block_digest": block_result,
                "payload_digest": payload_result,
            }
        )

    return {
        "count": len(records),
        "mismatch_count": mismatch_count,
        "records": records,
    }
