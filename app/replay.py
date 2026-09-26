"""WARC revisit replay core.

解析单个自包含 WARC（支持未压缩与 gzip 压缩，gzip 可为整条单流或
按记录拼接的多 member 形式），只保留 ``response`` 与 ``revisit`` 记录，
按 ``WARC-Record-ID`` 建索引，并把 ``revisit`` 沿 ``WARC-Refers-To``
引用链还原到终点 ``response`` 的正文。

任何输入/结构问题都抛 :class:`ReplayError`（由上层映射为 HTTP 422）。
"""

from __future__ import annotations

import base64
import binascii
import re
import zlib
from dataclasses import dataclass, field
from io import BytesIO
from typing import Optional

from warcio.archiveiterator import WARCIterator
from warcio.limitreader import LimitReader
from warcio.recordloader import ArcWarcRecordLoader

# 解码后 WARC 体积上限：2 MiB
MAX_DECODED_BYTES = 2 * 1024 * 1024

_VALID_WARC_VERSIONS = {"WARC/1.0", "WARC/1.1"}
_HANDLED_TYPES = ("response", "revisit")
_HTTP_STATUS_RE = re.compile(r"^\d{3}$")
# HTTP 请求方法（RFC 9110 token）与请求行剩余部分：请求目标 + HTTP 版本
# （/v1/http-exchanges 解析 request 记录用）
_METHOD_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_REQUEST_REST_RE = re.compile(r"^(\S+)\s+HTTP/\d+(?:\.\d+)?$")
# 去除外层空白；尖括号形式 <...> 去括号后再比较
_ANGLE_RE = re.compile(r"^[^\x00-\x20<>]+$")


class ReplayError(Exception):
    """可预期的输入错误，detail 为可直接返回给客户端的结构。

    默认映射为 HTTP 422；``status_code`` 可改为其他状态（如 navigate 的
    “无合格抓取”404）。
    """

    def __init__(self, code: str, message: str, *, status_code: int = 422,
                 **detail: object):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.detail = {"error": code, "message": message, **detail}


@dataclass
class _RawRecord:
    order: int
    record_id_raw: str
    record_id: str
    rec_type: str  # response / revisit
    target_uri: Optional[str]
    date: Optional[str]
    refers_to_raw: Optional[str]  # WARC-Refers-To 原始字面值
    refers_to: Optional[str]  # 规范化后的 WARC-Refers-To
    body: bytes
    http_status: Optional[int]
    http_location: Optional[str] = None  # HTTP Location 头原始值（重定向用）
    offset: int = 0
    body_source_id: Optional[str] = None  # 还原后正文来源记录 ID（原始字面值）
    # 记录在（解压后）归档字节流中的范围 (offset, length)：自 WARC 版本行起、
    # 含完整记录块、不含记录间空行；仅 parse_warc(capture_spans=True) 时填充
    span: Optional[tuple[int, int]] = None
    # WARC-Block-Digest / WARC-Payload-Digest 原始字面值（/v1/digest-audit 用）
    block_digest_raw: Optional[str] = None
    payload_digest_raw: Optional[str] = None
    # WARC-Concurrent-To 全部出现值：原始值与规范化值（仅
    # parse_warc(include_requests=True) 时填充，/v1/http-exchanges 配对用）
    concurrent_to_raw: list[str] = field(default_factory=list)
    concurrent_to: list[str] = field(default_factory=list)
    # request 记录的 HTTP 请求方法与请求目标（仅 include_requests 时解析）
    http_method: Optional[str] = None
    http_request_target: Optional[str] = None


@dataclass
class ResolvedRecord:
    """完成 revisit 正文还原后的记录；正文统一为链终点 response 的字节。"""

    order: int
    record_id_raw: str
    record_id: str
    rec_type: str  # response / revisit
    target_uri: Optional[str]
    date: Optional[str]
    http_status: Optional[int]
    http_location: Optional[str]
    body: bytes
    body_source_id: str


def decode_warc_payload(payload_b64: str) -> bytes:
    """解码 Base64 WARC，必要时解开 gzip，并强制 2 MiB 解码上限。"""
    # 标准 base64 字母表只允许 A-Za-z0-9+/= 与空白；先试标准表，
    # 再尝试 urlsafe 字母表（-_），二者皆非即非法输入。
    # 标准 base64 字母表只允许 A-Za-z0-9+/= 与空白；非 ASCII 字符直接非法。
    try:
        compact = b"".join(payload_b64.encode("ascii", "strict").split())
    except UnicodeEncodeError:
        raise ReplayError(
            "invalid_base64",
            "warc 字段不是合法的 Base64 编码数据",
        )
    decoded: Optional[bytes] = None
    # 先试标准字母表，再试 urlsafe 字母表（-_），二者皆非即非法输入。
    for alt in (False, True):
        try:
            padded = compact + b"=" * (-len(compact) % 4)
            decoded = base64.b64decode(
                padded, altchars=b"-_" if alt else None, validate=True
            )
            break
        except (binascii.Error, ValueError):
            continue
    if decoded is None:
        raise ReplayError(
            "invalid_base64",
            "warc 字段不是合法的 Base64 编码数据",
        )

    if len(decoded) > MAX_DECODED_BYTES:
        raise ReplayError(
            "payload_too_large",
            f"解码后 WARC 体积为 {len(decoded)} 字节，超过上限 "
            f"{MAX_DECODED_BYTES} 字节（2 MiB）",
            decoded_size=len(decoded),
            max_bytes=MAX_DECODED_BYTES,
        )

    return gunzip_if_needed(decoded)


def gunzip_if_needed(data: bytes) -> bytes:
    """按内容嗅探 gzip；支持多 member 拼接，并校验尾部垃圾。"""
    if len(data) < 2 or data[:2] != b"\x1f\x8b":
        return data

    out = bytearray()
    remaining = data
    try:
        while remaining[:2] == b"\x1f\x8b":
            d = zlib.decompressobj(31)
            chunk = d.decompress(remaining)
            chunk += d.flush()
            if not d.eof:
                raise ReplayError("malformed_warc", "gzip 数据被截断或已损坏")
            out.extend(chunk)
            remaining = d.unused_data
    except zlib.error as exc:
        raise ReplayError("malformed_warc", f"gzip 解压失败：{exc}")

    if remaining.strip():
        raise ReplayError("malformed_warc", "gzip 流之后存在无法解析的多余数据")

    if len(out) > MAX_DECODED_BYTES:
        raise ReplayError(
            "payload_too_large",
            f"解压后 WARC 体积为 {len(out)} 字节，超过上限 "
            f"{MAX_DECODED_BYTES} 字节（2 MiB）",
            decoded_size=len(out),
            max_bytes=MAX_DECODED_BYTES,
        )
    return bytes(out)


def _normalize_id(raw: str, *, field: str, order: int) -> str:
    """规范化 WARC ID：去空白、去尖括号。返回可比较形式。"""
    value = raw.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    value = value.strip()
    if not value or not _ANGLE_RE.match(value):
        raise ReplayError(
            "malformed_warc",
            f"第 {order} 条记录的 {field} 缺失或格式非法",
            record_order=order,
        )
    return value


def _http_status_from(rec) -> Optional[int]:
    """从 warcio 解析出的 HTTP 头中提取状态码；无有效 HTTP 响应头返回 None。"""
    hh = rec.http_headers
    if hh is None:
        return None
    if not str(getattr(hh, "protocol", "")).upper().startswith("HTTP/"):
        return None
    code = hh.get_statuscode()
    if not _HTTP_STATUS_RE.match(code or ""):
        return None
    return int(code)


def _http_location_from(rec) -> Optional[str]:
    """提取 HTTP ``Location`` 头原始值；无头/无该头/空值返回 None。"""
    hh = rec.http_headers
    if hh is None:
        return None
    if not str(getattr(hh, "protocol", "")).upper().startswith("HTTP/"):
        return None
    location = hh.get_header("Location")
    if location is None:
        return None
    location = location.strip()
    return location or None


def _all_headers(headers, name: str) -> list[str]:
    """返回指定 WARC 头字段的全部出现值（字段名大小写不敏感）。

    warcio 的 ``get_header`` 只返回首个匹配；``WARC-Concurrent-To`` 允许
    重复出现，配对时需要逐条读取（同一记录声明多个不同伙伴属错误）。
    """
    lname = name.lower()
    return [value for (k, value) in headers.headers if k.lower() == lname]


def _http_request_line_from(rec) -> tuple[Optional[str], Optional[str]]:
    """从 request 记录的 HTTP 头提取 ``(请求方法, 请求目标)``。

    warcio 以 ``verify_http=False`` 解析请求行：``protocol`` 为首个空白前
    的词（请求方法），``statusline`` 为其余部分（请求目标与 HTTP 版本）。
    缺 HTTP 头、缺方法/请求目标或无 ``HTTP/<版本>`` 时返回 ``(None, None)``。
    """
    hh = rec.http_headers
    if hh is None:
        return None, None
    method = str(getattr(hh, "protocol", "") or "")
    rest = str(getattr(hh, "statusline", "") or "")
    match = _REQUEST_REST_RE.match(rest)
    if not _METHOD_RE.match(method) or match is None:
        return None, None
    return method, match.group(1)


def _verify_block_length(rec, body: bytes, order: int, offset: int,
                         raw_id: Optional[str]) -> None:
    """核对记录块实际读到的字节数达到声明的 WARC Content-Length。

    warcio 用 ``LimitReader`` 按 Content-Length 限长读取；底层流提前 EOF
    （记录被截断）时它静默返回短数据，不抛错。这里在整条记录读完后比对
    实际消费量与声明长度。消费量经 ``LimitReader.tell()`` 取得，包含
    warcio 解析时已预读的 HTTP 头，因此 response/request/revisit 记录的
    HTTP 头部分不会被漏算。
    """
    declared = rec.length  # 解析后的 Content-Length；无此头时为 None
    if declared is None:
        return  # 未声明长度，无法核对（warcio 也未限长）

    raw_stream = rec.raw_stream
    if isinstance(raw_stream, LimitReader):
        consumed = raw_stream.tell()
    else:
        # 兜底：非 LimitReader 包装时，用 warcio 记录的负载长度反推
        # 已预读的 HTTP 头字节数，再加上本次读到的正文。
        http_len = declared - rec.payload_length if rec.payload_length >= 0 else 0
        consumed = http_len + len(body)

    if consumed >= declared:
        return

    detail: dict = {
        "record_order": order,
        "offset": offset,
        "declared_length": declared,
        "actual_length": consumed,
    }
    record_id = raw_id.strip() if raw_id and raw_id.strip() else None
    if record_id is not None:
        detail["record_id"] = record_id
    raise ReplayError(
        "malformed_warc",
        f"第 {order} 条记录块被截断：声明 Content-Length 为 {declared} 字节，"
        f"实际仅读到 {consumed} 字节",
        **detail,
    )


def _raise_truncated_record(order: int, offset: int, declared: Optional[int],
                            actual: Optional[int], record_id: Optional[str],
                            *, headers_complete: bool) -> None:
    """统一抛出“末记录被截断”的 malformed_warc 错误。"""
    detail: dict = {"record_order": order, "offset": offset}
    if record_id:
        detail["record_id"] = record_id
    if declared is not None:
        detail["declared_length"] = declared
    if actual is not None:
        detail["actual_length"] = actual
    if headers_complete:
        message = (
            f"第 {order} 条记录块被截断：声明 Content-Length 为 {declared} 字节，"
            f"实际仅读到 {actual} 字节"
        )
    else:
        message = (
            f"第 {order} 条记录头部未结束即遇到 EOF，但已声明 "
            f"Content-Length 为 {declared} 字节"
        )
    raise ReplayError("malformed_warc", message, **detail)


def _check_trailing_truncation(data: bytes) -> None:
    """识别 warcio 会静默丢弃的、未交出的截断末记录。

    ``WARCIterator`` 在加载下一条记录时若收到 ``EOFError``，会把它当作
    “归档正常结束”而直接停止迭代，既不交出该记录也不报错。因此当末记录
    的 WARC 头完整、``Content-Length`` 为正、但头后直接 EOF（0 字节块，
    或剩余块字节不足）时，包括它跟在完整记录之后、以及单流/多 member
    gzip 解压后的情形，接口会误回 200 和空结果（或仅返回前序记录）。

    这里按 WARC 记录边界做一次最小预扫：只在能确定“存在一条未交出的
    截断记录”时抛 malformed_warc（附记录序号，尽量附 WARC-Record-ID）；
    其余所有语法判断仍交给 warcio，避免与之产生分歧。合法的空正文
    （Content-Length: 0）、无末尾 ``\\r\\n\\r\\n`` 分隔符的完整记录等
    均不受影响。
    """
    pos = 0
    order = 0
    total = len(data)
    while pos < total:
        # 记录之间允许有任意数量空行；归档开头若就是空白/垃圾，交给 warcio。
        line_end = data.find(b"\n", pos)
        if line_end == -1:
            break
        first = data[pos:line_end].rstrip()
        if not first:
            pos = line_end + 1
            continue
        # 任何非空记录边界都应以 WARC 版本行开始；否则（归档开头是垃圾、
        # Content-Length 错位落到非 WARC 行、记录间有杂散文本等）交给 warcio。
        if not first.upper().startswith(b"WARC/"):
            return

        rec_offset = pos
        content_length: Optional[int] = None
        record_id: Optional[str] = None
        headers_complete = False
        scan = line_end + 1
        while True:
            nl = data.find(b"\n", scan)
            if nl == -1:
                line = data[scan:]
                scan = total
                next_scan = total
            else:
                line = data[scan:nl]
                next_scan = nl + 1
            stripped = line.rstrip()
            if not stripped:
                headers_complete = True
                scan = next_scan
                break
            colon = line.find(b":")
            if colon != -1:
                name = line[:colon].strip().lower()
                value_b = line[colon + 1:].strip()
                if name == b"content-length" and content_length is None:
                    try:
                        parsed = int(value_b.decode("ascii"))
                    except (ValueError, UnicodeDecodeError):
                        parsed = None
                    # 负数等异常取值不预判，交给 warcio 判畸形
                    if parsed is not None and parsed >= 0:
                        content_length = parsed
                elif name == b"warc-record-id" and record_id is None:
                    try:
                        rid = value_b.decode("utf-8").strip()
                    except UnicodeDecodeError:
                        rid = ""
                    if rid:
                        record_id = rid
            if nl == -1:
                break
            scan = next_scan

        if not headers_complete:
            # 记录头未以空行结束。只有已明确读到正的 Content-Length 时才能
            # 断定这是一条头部不完整的截断记录（块必然完全缺失）。
            if content_length and content_length > 0:
                _raise_truncated_record(
                    order, rec_offset, content_length, None, record_id,
                    headers_complete=False,
                )
            return  # 其余情形交给 warcio（通常为 truncated headers → 422）

        if content_length and content_length > 0:
            available = total - scan
            if available < content_length:
                _raise_truncated_record(
                    order, rec_offset, content_length, available, record_id,
                    headers_complete=True,
                )
            # 块字节充足：跳过声明长度的块体，继续检查后续记录（截断记录
            # 可能跟在一条完整记录之后）；落点若不在下一条 WARC 记录边界
            # （如 Content-Length 与实际错位），交由 warcio 判定。
            pos = scan + content_length
        else:
            # Content-Length 为 0/缺失：空块即合法，从头结束处继续。
            pos = scan
        order += 1


class _MissingTargetURIRecordLoader(ArcWarcRecordLoader):
    """容忍缺 ``WARC-Target-URI`` 的记录加载器（仅供 navigate 使用）。

    warcio 1.8 的 ``load_http_headers`` 对 response/revisit 记录直接调用
    ``uri.startswith(...)``；URI 头缺失（``uri is None``）时抛
    ``AttributeError``，使整条归档被判畸形。缺 URI 的记录在 navigate 中
    本就不参与时点选择，这里仅为通过 warcio 的 scheme 判定而填入占位
    http URI，让其照常解析记录自带的 HTTP 头（不解析反而会让后续
    ``response_without_http_headers`` 校验误伤这条记录）；记录的
    ``WARC-Target-URI`` 字段本身保持缺失不变，由调用方据此排除。
    """

    def load_http_headers(self, rec_type, uri, stream, length):
        if uri is None and rec_type in self.HTTP_RECORDS:
            uri = "http://missing-warc-target-uri.invalid/"
        return super().load_http_headers(rec_type, uri, stream, length)


def parse_warc(data: bytes, *, allow_missing_target_uri: bool = False,
               capture_spans: bool = False,
               include_requests: bool = False) -> list[_RawRecord]:
    """解析 WARC 为原始记录序列（默认仅含 response/revisit）。

    跳过 warcinfo/request/metadata 等类型（``include_requests`` 为真时
    保留 request）；任何结构性损坏抛 ``malformed_warc``。

    ``allow_missing_target_uri`` 为真时（navigate），缺
    ``WARC-Target-URI`` 的记录不再触发 warcio 的 ``None.startswith``
    崩溃，而是作为 ``target_uri=None`` 保留（由调用方排除出选择索引）；
    截断、缺/重复记录 ID、revisit 引用链等其余结构校验一律不变。

    ``capture_spans`` 为真时（/v1/extract），额外为每条 response/revisit
    记录记录其在 ``data`` 中的原始字节范围（``span`` 字段，供按原字节
    切出写出）。

    ``include_requests`` 为真时（/v1/http-exchanges），``request`` 记录
    一并保留（同样要求 WARC-Record-ID，缺失即 missing_record_id），并为
    request/response/revisit 记录解析全部 ``WARC-Concurrent-To`` 出现值
    与 request 记录的 HTTP 请求方法、请求目标；其余解析与校验行为不变。
    """
    _check_trailing_truncation(data)

    records: list[_RawRecord] = []
    stream = BytesIO(data)
    total_order = 0  # 归档中的真实记录序号（含被跳过的类型）
    # /v1/http-exchanges 把 request 记录纳入配对全集；其余接口保持只
    # 保留 response/revisit
    handled_types = (
        _HANDLED_TYPES + ("request",) if include_requests else _HANDLED_TYPES
    )
    try:
        # WARCIterator 强制 WARC 格式解析：首行非 WARC 版本行会直接报错，
        # 不会把任意文本宽松回退成 ARC/WARC/1.0 记录。
        iterator = WARCIterator(stream, arc2warc=False)
        if allow_missing_target_uri:
            iterator.loader = _MissingTargetURIRecordLoader(arc2warc=False)
        for rec in iterator:
            order = total_order
            total_order += 1
            warc_headers = rec.rec_headers
            protocol = str(getattr(warc_headers, "protocol", ""))
            if protocol not in _VALID_WARC_VERSIONS:
                raise ReplayError(
                    "malformed_warc",
                    f"第 {order} 条记录不是合法的 WARC/1.0 或 WARC/1.1 记录",
                    record_order=order,
                    offset=getattr(iterator, "offset", None),
                )

            rec_type = rec.rec_type
            # 字段行解析失败（如 "WARC-Type response" 无冒号、首行不是
            # WARC 版本行但被 warcio 宽松接受）时 rec_type 为 None。
            if rec_type is None:
                raise ReplayError(
                    "malformed_warc",
                    f"第 {order} 条记录的 WARC 头部字段损坏，无法识别记录类型",
                    record_order=order,
                    offset=getattr(iterator, "offset", None),
                )

            offset = getattr(iterator, "offset", 0)

            # 每条记录（包括 warcinfo/request 等不参与还原的类型）都必须
            # 读足声明的 Content-Length；短读即截断，判 malformed_warc。
            # raw_stream 为 HTTP 头之后的原始块字节：保留归档原貌，
            # 不解 Content-Encoding、不拆 Transfer-Encoding: chunked。
            try:
                body = rec.raw_stream.read()
            except Exception as exc:  # warcio 读正文时可能抛 LimitReader 异常
                raise ReplayError(
                    "malformed_warc",
                    f"第 {order} 条记录正文读取失败：{exc}",
                    record_order=order,
                    offset=offset,
                )
            _verify_block_length(
                rec, body, order, offset,
                warc_headers.get_header("WARC-Record-ID"),
            )

            if rec_type not in handled_types:
                continue

            raw_id = warc_headers.get_header("WARC-Record-ID")
            if not raw_id or not raw_id.strip():
                raise ReplayError(
                    "missing_record_id",
                    f"第 {order} 条 {rec_type} 记录缺少 WARC-Record-ID",
                    record_order=order,
                    offset=offset,
                    record_type=rec_type,
                )
            norm_id = _normalize_id(raw_id, field="WARC-Record-ID", order=order)

            refers_raw = warc_headers.get_header("WARC-Refers-To")
            refers_norm: Optional[str] = None
            if refers_raw is not None:
                refers_norm = _normalize_id(
                    refers_raw, field="WARC-Refers-To", order=order
                )

            span: Optional[tuple[int, int]] = None
            if capture_spans:
                # get_record_offset/length 触发 iterator 的 read_to_end
                # （读完记录余量、消费记录间空行、定位下一条记录）。此处
                # 调用与迭代器自身的延迟调用完全等效，不改变解析行为；
                # 且须在正文读完、_verify_block_length 之后调用（其依赖
                # raw_stream.tell()，正文读完后 tell 不再变化）。
                span = (iterator.get_record_offset(),
                        iterator.get_record_length())

            # 仅 /v1/http-exchanges 需要：request 记录的请求行与双方的
            # WARC-Concurrent-To（不影响其余接口的既有行为）
            http_method: Optional[str] = None
            http_request_target: Optional[str] = None
            concurrent_raw: list[str] = []
            concurrent_norm: list[str] = []
            if include_requests:
                if rec_type == "request":
                    http_method, http_request_target = _http_request_line_from(
                        rec
                    )
                for value in _all_headers(warc_headers, "WARC-Concurrent-To"):
                    concurrent_raw.append(value.strip())
                    concurrent_norm.append(
                        _normalize_id(
                            value, field="WARC-Concurrent-To", order=order
                        )
                    )

            records.append(
                _RawRecord(
                    order=order,
                    record_id_raw=raw_id.strip(),
                    record_id=norm_id,
                    rec_type=rec_type,
                    target_uri=warc_headers.get_header("WARC-Target-URI"),
                    date=warc_headers.get_header("WARC-Date"),
                    refers_to_raw=refers_raw.strip() if refers_raw else None,
                    refers_to=refers_norm,
                    body=body,
                    http_status=_http_status_from(rec),
                    http_location=_http_location_from(rec),
                    offset=offset,
                    span=span,
                    block_digest_raw=warc_headers.get_header("WARC-Block-Digest"),
                    payload_digest_raw=warc_headers.get_header(
                        "WARC-Payload-Digest"
                    ),
                    concurrent_to_raw=concurrent_raw,
                    concurrent_to=concurrent_norm,
                    http_method=http_method,
                    http_request_target=http_request_target,
                )
            )
    except ReplayError:
        raise
    except Exception as exc:
        # warcio 的 ArchiveLoadFailed / StatusAndHeadersParserException /
        # 偶发的 AttributeError（截断头部）等统一归为畸形 WARC。
        raise ReplayError(
            "malformed_warc",
            f"WARC 解析失败：{type(exc).__name__}: {exc}".rstrip(),
        )

    return records


def _validate_chain(raw: _RawRecord, by_id: dict[str, _RawRecord]) -> tuple[list[str], _RawRecord]:
    """沿 WARC-Refers-To 走链。

    返回 ``(引用链, 终点记录)``。引用链包含 revisit 自身及每一跳的
    原始 ID 字面值；引用缺失时最后一个元素为缺失的目标字面值。
    """
    chain = [raw.record_id_raw]
    current = raw
    seen: set[str] = set()

    while current.rec_type == "revisit":
        if current.record_id in seen:
            # chain 已追加过指回当前节点的那条边（current 的 ID 已在
            # chain 末尾），直接在重复节点处闭合引用链，不再重复追加。
            raise ReplayError(
                "reference_cycle",
                f"记录 {raw.record_id_raw} 的 WARC-Refers-To 引用链成环",
                record_id=raw.record_id_raw,
                chain=chain,
            )
        seen.add(current.record_id)

        if not current.refers_to:
            raise ReplayError(
                "chain_broken",
                f"revisit 记录 {current.record_id_raw} 缺少 WARC-Refers-To，"
                "无法定位引用目标",
                record_id=raw.record_id_raw,
                broken_record_id=current.record_id_raw,
                chain=chain,
            )

        target = by_id.get(current.refers_to)
        if target is None:
            chain.append(current.refers_to_raw)  # type: ignore[arg-type]
            raise ReplayError(
                "target_not_found",
                f"记录 {current.record_id_raw} 通过 WARC-Refers-To 引用的 "
                f"{current.refers_to_raw} 在包内不存在",
                record_id=raw.record_id_raw,
                missing_id=current.refers_to_raw,
                chain=chain,
            )

        chain.append(current.refers_to_raw)  # type: ignore[arg-type]
        current = target

    # 链终点不是 response（理论上 response/revisit 之外的类型已被过滤，
    # 这里仍做防御性处理）。
    if current.rec_type != "response":
        raise ReplayError(
            "invalid_target_type",
            f"记录 {raw.record_id_raw} 的引用链终点类型为 {current.rec_type}，"
            "终点必须为 response",
            record_id=raw.record_id_raw,
            chain=chain,
            endpoint_record_id=current.record_id_raw,
            endpoint_record_type=current.rec_type,
        )

    if current.http_status is None:
        raise ReplayError(
            "endpoint_without_http_headers",
            f"记录 {raw.record_id_raw} 的引用链终点 {current.record_id_raw} "
            "不是有效的 HTTP 响应（缺少 HTTP 响应头或状态行）",
            record_id=raw.record_id_raw,
            chain=chain,
            endpoint_record_id=current.record_id_raw,
        )

    return chain, current


def validate_records(raws: list[_RawRecord]) -> dict[str, _RawRecord]:
    """按 WARC-Record-ID 建索引并完成全部结构校验。

    检查包内重复 ID、response 记录自身的 HTTP 响应头、revisit 的
    WARC-Refers-To 引用链（断链/缺失目标/成环/终点类型与终点 HTTP 头）。
    返回规范化 ID -> 记录的索引。:func:`load_records` 与 /v1/extract
    共用这一套校验，保证各接口对同一归档的判定完全一致。
    """
    by_id: dict[str, _RawRecord] = {}
    for raw in raws:
        if raw.record_id in by_id:
            raise ReplayError(
                "duplicate_record_id",
                f"WARC-Record-ID {raw.record_id_raw} 在包内重复出现",
                record_id=raw.record_id_raw,
                first_record_order=by_id[raw.record_id].order,
                duplicate_record_order=raw.order,
            )
        by_id[raw.record_id] = raw

    for raw in raws:
        if raw.rec_type == "response":
            if raw.http_status is None:
                raise ReplayError(
                    "response_without_http_headers",
                    f"response 记录 {raw.record_id_raw} 缺少有效的 HTTP 响应头"
                    "或状态行",
                    record_id=raw.record_id_raw,
                    record_order=raw.order,
                )
        else:
            _validate_chain(raw, by_id)
    return by_id


def load_records(warc_b64: str, *, allow_missing_target_uri: bool = False
                 ) -> list["ResolvedRecord"]:
    """解码并解析 WARC，完成全部结构校验与 revisit 正文还原。

    与 :func:`replay` 共用同一套 WARC 校验规则（含 2 MiB 上限）。返回的
    记录保留 response/revisit 自身的全部字段（状态、Location 等），正文已
    统一解析为链终点 response 的字节，``body_source_id`` 为其原始 ID。

    ``allow_missing_target_uri`` 为真时（navigate），缺
    ``WARC-Target-URI`` 的记录予以保留（``target_uri`` 为 None，由 navigate
    排除出时点选择），而不是让 warcio 把整条归档判为畸形；其余校验不变。
    """
    data = decode_warc_payload(warc_b64)
    raws = parse_warc(data, allow_missing_target_uri=allow_missing_target_uri)
    by_id = validate_records(raws)

    resolved: list[ResolvedRecord] = []
    for raw in raws:
        if raw.rec_type == "response":
            endpoint = raw
        else:
            # revisit：保留自己的 URL/时间/状态/Location，正文只继承链终点
            # response，不借用终点的状态或 Location。
            _, endpoint = _validate_chain(raw, by_id)

        resolved.append(
            ResolvedRecord(
                order=raw.order,
                record_id_raw=raw.record_id_raw,
                record_id=raw.record_id,
                rec_type=raw.rec_type,
                target_uri=raw.target_uri,
                date=raw.date,
                http_status=raw.http_status,
                http_location=raw.http_location,
                body=endpoint.body,
                body_source_id=endpoint.record_id_raw,
            )
        )

    return resolved


def replay(warc_b64: str) -> list[dict]:
    """完整重放入口，返回按归档原序排列的记录结果。"""
    return [
        {
            "order": rec.order,
            "record_id": rec.record_id_raw,
            "record_type": rec.rec_type,
            "target_uri": rec.target_uri,
            "capture_time": rec.date,
            "http_status": rec.http_status,
            "body_base64": base64.b64encode(rec.body).decode("ascii"),
            "body_source_id": rec.body_source_id,
        }
        for rec in load_records(warc_b64)
    ]
