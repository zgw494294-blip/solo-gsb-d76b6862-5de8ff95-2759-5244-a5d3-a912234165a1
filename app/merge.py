"""双归档合并（POST /v1/merge）核心。

接收 left_warc、right_warc 两份 Base64 WARC（各自解码后 ≤ 2 MiB），把两包
的 response/revisit 记录按“左包原序 + 右包原序”合成一份可回放归档：

- 两份归档各自完整经过与 /v1/replay 相同的解码（2 MiB 上限、gzip 嗅探）
  与结构/引用链校验（先左后右）——任一包的任何错误都使整个请求 422；
- 合并包只含 response/revisit 记录（warcinfo/request 等类型不纳入），
  记录以原始字节写出，WARC/HTTP 头与负载逐字节保留，记录间以
  ``CRLF CRLF`` 分隔，合并包不压缩；
- 两包之间出现相同的 ``WARC-Record-ID``（按现有规范化规则：去空白、去
  尖括号）时，**只为右包的冲突记录**生成确定性、全包唯一的新 ID
  （``urn:uuid:`` 形式的 UUIDv5：同一旧 ID 永远映射到同一新 ID，重复
  请求结果一致），并同步改写右包内部指向它的 ``WARC-Refers-To``；
  左包记录与其余右包记录的字节保持不变；
- 改名按**完整字段值**进行：WARC 允许字段值折叠在以空格/制表符开头的
  续行上（warcio 解析时把续行拼进值），若目标头带有续行，改写会把整个
  字段（首行连同全部续行）收敛为单行新值——续行中残留的旧值必须清除，
  否则合并包会带着新旧两段 ID，回放时既匹配不到新记录、也匹配不到旧
  记录。仅当某字段确实需要改名（冲突的 ``WARC-Record-ID``、或指向改名
  记录的 ``WARC-Refers-To``）时才动字节，无冲突记录逐字节保留；
- 头切分、续行分组与补长对结束行的判定与 warcio
  ``StatusAndHeadersParser`` 完全一致：readline 读出的整行经
  ``rstrip()`` 后为空即结束头部，因此仅含空格/制表符的行
  （``" \\r\\n"``、``"\\t\\n"`` 等）是结束行而不是上一字段的续行
  （CRLF、LF 均支持）。改名只删除目标字段的真实续行，结束行及其余
  WARC/HTTP 头与记录块原字节一律保留；
- 写出前对合并包再做一次完整的解码同口径结构/引用链校验（含记录数与 ID
  集合一致性核对）：改名或拼接一旦产生无法回放的归档，返回 422 而不是
  交付“伪成功”的 merged_warc；
- 左包末记录缺 ``Content-Length`` 时（其记录块延伸至左包 EOF），若其后
  还有右包记录，为该记录注入 ``Content-Length``（取实际块长），否则
  拼接后它的块会延伸至合并包 EOF、吞掉全部右包记录；
- 合并包（未压缩）超过 2 MiB → 422 ``payload_too_large``。

因此把合并包交给 /v1/replay 后，各记录的 HTTP 状态与还原正文与原包
一致，revisit 的正文来源 ID 随改名映射替换。
"""

from __future__ import annotations

import base64
import uuid
from typing import Optional

from .replay import (
    MAX_DECODED_BYTES,
    ReplayError,
    _RawRecord,
    decode_warc_payload,
    parse_warc,
    validate_records,
)

# WARC 记录分隔符：CRLF CRLF
_RECORD_SEPARATOR = b"\r\n\r\n"

# 改名用的固定 UUIDv5 命名空间：纯计算得出，保证同一旧 ID 永远映射到
# 同一新 ID（重复请求映射一致）
_MERGE_ID_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://warc-replay.local/v1/merge"
)


def _record_chunk(data: bytes, raw: _RawRecord) -> bytes:
    """按 span 切出记录原始字节（防御性剥去范围开头的空行，同 extract）。"""
    span = raw.span
    assert span is not None  # capture_spans=True 时必然已填充
    offset, length = span
    return data[offset:offset + length].lstrip(b"\r\n")


def _is_blank_line(line: bytes) -> bool:
    """是否为头部结束空行：去掉全部行尾/空白后为空。

    与 warcio ``StatusAndHeadersParser`` 的口径一致：其
    ``_strip_count`` 对 readline 读出的整行做 ``rstrip()``，故仅含
    空格/制表符的行（``" \\r\\n"``、``"\\t\\n"`` 等）同样终止头部，
    而不是被折叠成上一个字段的续行；CRLF、LF 均支持。
    """
    return not line.rstrip()


def _split_head(record: bytes) -> tuple[list[bytes], bytes]:
    """把记录字节切成 WARC 头部分（含版本行与结尾空行）与记录块。

    返回 ``(头部分各行, 记录块)``。通过解析校验的记录头部必然以空行
    （含仅含空格/制表符的行，见 :func:`_is_blank_line`）结束；找不到
    空行时把整条记录视为头部、块为空（防御性兜底）。
    """
    lines: list[bytes] = []
    pos = 0
    while pos < len(record):
        nl = record.find(b"\n", pos)
        if nl == -1:
            lines.append(record[pos:])
            return lines, b""
        line = record[pos:nl + 1]
        lines.append(line)
        pos = nl + 1
        if len(lines) > 1 and _is_blank_line(line):
            return lines, record[pos:]
    return lines, b""


def _line_ending(line: bytes) -> bytes:
    """该行的换行符（\\r\\n 或 \\n）；无换行符返回空。"""
    if line.endswith(b"\r\n"):
        return b"\r\n"
    if line.endswith(b"\n"):
        return b"\n"
    return b""


def _header_name_raw(line: bytes) -> bytes:
    """头部字段名原样（保留大小写）：首个冒号前、去尾部空白。"""
    return line.rstrip(b"\r\n").partition(b":")[0].rstrip(b" \t")


def _header_name(line: bytes) -> bytes:
    """头部字段名（小写），与 warcio 的名称比较口径一致。"""
    return _header_name_raw(line).lower()


def _ensure_content_length(record: bytes) -> bytes:
    """缺 Content-Length 的记录注入实际块长；已有（或头部畸形）则原样返回。

    注入值 = 记录块实际字节数（头部分空行之后的全部字节），使记录块
    不再延伸至 EOF，拼接后续记录后边界仍然正确；块内字节保持不变。
    """
    lines, block = _split_head(record)
    if len(lines) < 2 or not _is_blank_line(lines[-1]):
        return record  # 头部没有空行结束（不会发生于通过校验的记录）
    for line in lines[1:-1]:
        if _header_name(line) == b"content-length":
            return record
    # 新行换行符沿用结束行：仅含空格/制表符的结束行也带 CRLF/LF 行尾；
    # 头部恰在无换行符处结束（EOF）的防御性情形才回退 CRLF。
    ending = _line_ending(lines[-1]) or _line_ending(lines[0]) or b"\r\n"
    inject = b"Content-Length: " + str(len(block)).encode("ascii") + ending
    return b"".join(lines[:-1]) + inject + lines[-1] + block


def _is_continuation(line: bytes) -> bool:
    """是否为 WARC 头字段续行：非空行且行首（换行符之前）是空格或制表符。

    与 warcio ``StatusAndHeadersParser`` 的口径一致：续行拼入上一个字段
    的值；空行（含仅含空格/制表符的结束行）先终止头部，绝不会被当作
    续行折叠。
    """
    body = line.rstrip()
    return bool(body) and body[:1] in (b" ", b"\t")


def _replace_header_value(line: bytes, new_value: bytes) -> bytes:
    """保留原字段名（含大小写与冒号前空白）、冒号后缩进与行尾，只替换值。"""
    if line.endswith(b"\r\n"):
        body, ending = line[:-2], b"\r\n"
    elif line.endswith(b"\n"):
        body, ending = line[:-1], b"\n"
    else:
        body, ending = line, b""
    name_part, sep, rest = body.partition(b":")
    if not sep:
        return line  # 无冒号行不可能是命名字段（防御性，不会发生于有效记录）
    indent = rest[:len(rest) - len(rest.lstrip(b" \t"))]
    return name_part + sep + indent + new_value + ending


def _field_group_end(lines: list[bytes], index: int) -> int:
    """返回 ``index`` 处字段所占行区间的结束下标（不含）。

    区间为字段首行加上其全部续行；头部结束空行（``lines[-1]``）之前的
    非续行行停止折叠。
    """
    end = index + 1
    last = len(lines) - 1
    while end < last and _is_continuation(lines[end]):
        end += 1
    return end


def _rewrite_refs(record: bytes, new_record_id: Optional[bytes],
                  new_refers_to: Optional[bytes]) -> bytes:
    """按完整字段值改写 WARC 头中 WARC-Record-ID / WARC-Refers-To。

    只触及 WARC 头部需要改名的字段（首个匹配，与 get_header 一致）：
    字段名（含大小写与冒号前空白）、冒号后缩进与行尾保持原样，字段整体
    （首行 + 全部续行）收敛为单行新值，续行中折叠的旧值随之删除；其余
    WARC/HTTP 头与记录块逐字节保留。兼容 CRLF、LF 两种行尾（新行沿用
    该字段首行的换行符）。
    """
    lines, block = _split_head(record)
    if len(lines) < 2 or not _is_blank_line(lines[-1]):
        return record  # 头部没有空行结束（不会发生于通过校验的记录）

    head_lines = lines[:-1]  # 版本行 + 字段行（含可能的续行）
    terminator = lines[-1]
    out: list[bytes] = [head_lines[0]]  # WARC 版本行原样保留
    i = 1
    while i < len(head_lines):
        line = head_lines[i]
        name = _header_name(line)
        new_value: Optional[bytes] = None
        if new_record_id is not None and name == b"warc-record-id":
            new_value = new_record_id
            new_record_id = None  # 与 get_header 一致：只改首个匹配
        elif new_refers_to is not None and name == b"warc-refers-to":
            new_value = new_refers_to
            new_refers_to = None
        if new_value is not None:
            # 字段整体（首行 + 全部续行）收敛为单行新值，丢弃续行中旧值
            end = _field_group_end(lines, i)
            out.append(_replace_header_value(line, new_value))
            i = end
            continue
        out.append(line)
        i += 1
    out.append(terminator)
    return b"".join(out) + block


def _new_record_id(old_norm: str, used: set[str]) -> str:
    """为冲突的旧 ID 生成确定性的新规范化 ID（``urn:uuid:`` 形式的 UUIDv5）。

    同一旧 ID 永远得到同一新 ID；万一与包内已有 ID 相撞，追加计数种子
    重试，直到全包唯一。
    """
    attempt = 0
    while True:
        seed = old_norm if attempt == 0 else f"{old_norm}#{attempt}"
        candidate = "urn:uuid:" + str(uuid.uuid5(_MERGE_ID_NAMESPACE, seed))
        if candidate not in used:
            return candidate
        attempt += 1


def merge(left_warc_b64: str, right_warc_b64: str) -> dict:
    """merge 入口：返回 count / merged_warc / renamed；错误抛 ReplayError。"""
    # 先左后右：各自完整解码 + 结构/引用链校验，任一包有错即 422
    left_data = decode_warc_payload(left_warc_b64)
    left_raws = parse_warc(left_data, capture_spans=True)
    left_by_id = validate_records(left_raws)

    right_data = decode_warc_payload(right_warc_b64)
    right_raws = parse_warc(right_data, capture_spans=True)
    right_by_id = validate_records(right_raws)

    # 跨包冲突（按规范化 ID 比较）：只为右包冲突记录生成确定性新 ID
    used = set(left_by_id) | set(right_by_id)
    id_map: dict[str, str] = {}  # 右包规范化旧 ID -> 规范化新 ID
    renamed: list[dict] = []
    for raw in right_raws:
        if raw.record_id in left_by_id:
            new_norm = _new_record_id(raw.record_id, used)
            used.add(new_norm)
            id_map[raw.record_id] = new_norm
            renamed.append({
                "old_id": raw.record_id_raw,
                "new_id": f"<{new_norm}>",
            })

    chunks: list[bytes] = []
    for index, raw in enumerate(left_raws):
        chunk = _record_chunk(left_data, raw)
        # 左包末记录缺 Content-Length 且后面还有右包记录：注入实际块长，
        # 否则其记录块会延伸至合并包 EOF，吞掉全部右包记录
        if right_raws and index == len(left_raws) - 1:
            chunk = _ensure_content_length(chunk)
        chunks.append(chunk)

    for raw in right_raws:
        chunk = _record_chunk(right_data, raw)
        new_id = id_map.get(raw.record_id)
        new_refers = id_map.get(raw.refers_to) if raw.refers_to else None
        if new_id is not None or new_refers is not None:
            chunk = _rewrite_refs(
                chunk,
                f"<{new_id}>".encode("ascii") if new_id is not None else None,
                f"<{new_refers}>".encode("ascii")
                if new_refers is not None else None,
            )
        chunks.append(chunk)

    # 记录间以 CRLF CRLF 分隔；末条记录之后不加分隔符——缺 Content-Length
    # 的末记录块延伸至 EOF，追加任何字节都会改变其还原正文（同 extract）
    merged = _RECORD_SEPARATOR.join(chunks)
    if len(merged) > MAX_DECODED_BYTES:
        raise ReplayError(
            "payload_too_large",
            f"合并后 WARC 体积为 {len(merged)} 字节，超过上限 "
            f"{MAX_DECODED_BYTES} 字节（2 MiB）",
            decoded_size=len(merged),
            max_bytes=MAX_DECODED_BYTES,
        )

    _validate_merged(merged, left_raws, right_raws, id_map)

    return {
        "count": len(left_raws) + len(right_raws),
        "merged_warc": base64.b64encode(merged).decode("ascii"),
        "renamed": renamed,
    }


def _validate_merged(merged: bytes, left_raws: list[_RawRecord],
                     right_raws: list[_RawRecord],
                     id_map: dict[str, str]) -> None:
    """交付前对合并包做与 /v1/replay 同口径的完整校验。

    改名（尤其带续行的头）或拼接理论上都应保持归档可回放；这里以实际解析
    结果兜底：记录数、记录顺序、规范化 ID 集合与预期逐一核对，引用链交给
    :func:`validate_records` 验证。任一项不符就返回 422，绝不交付 200 +
    无法回放的 merged_warc。
    """
    expected_order = [raw.record_id for raw in left_raws]
    expected_order += [id_map.get(raw.record_id, raw.record_id)
                       for raw in right_raws]
    expected_ids = set(expected_order)

    try:
        merged_raws = parse_warc(merged, capture_spans=True)
        merged_by_id = validate_records(merged_raws)
    except ReplayError as exc:
        extra = {k: v for k, v in exc.detail.items()
                 if k not in ("error", "message")}
        raise ReplayError(
            "invalid_merged_warc",
            "合并生成的 WARC 未通过回放校验，拒绝交付："
            + exc.detail["message"],
            cause=exc.code,
            **extra,
        )

    actual_order = [raw.record_id for raw in merged_raws]
    if actual_order != expected_order or set(merged_by_id) != expected_ids:
        raise ReplayError(
            "invalid_merged_warc",
            "合并生成的 WARC 记录数、顺序或记录 ID 与合并预期不一致，"
            "拒绝交付",
            expected_count=len(expected_order),
            actual_count=len(actual_order),
            expected_ids=sorted(expected_ids),
            actual_ids=sorted(merged_by_id),
        )
