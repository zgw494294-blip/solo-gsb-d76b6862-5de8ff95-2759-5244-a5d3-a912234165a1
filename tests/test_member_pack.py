"""POST /v1/member-pack 端到端测试：多 member gzip 打包、按记录定位、
固定时间戳确定性、缺 Content-Length 末记录补长、2 MiB 上限与回放等价。"""

from __future__ import annotations

import base64
import gzip
import io
import struct

from fastapi.testclient import TestClient
from warcio.archiveiterator import ArchiveIterator

from app.main import app
from app.replay import MAX_DECODED_BYTES

client = TestClient(app)


# ---------- WARC 构造辅助（与 test_merge.py 同风格） ----------


def raw_record(rec_id="<urn:uuid:r>", rec_type="response", block=b"",
               extra_headers=b"", date="2020-01-01T00:00:00Z",
               uri="http://example.com/"):
    return (
        f"WARC/1.1\r\nWARC-Type: {rec_type}\r\n"
        f"WARC-Record-ID: {rec_id}\r\nWARC-Date: {date}\r\n"
        f"WARC-Target-URI: {uri}\r\n"
        f"Content-Length: {len(block)}\r\n".encode()
        + extra_headers + b"\r\n" + block + b"\r\n\r\n"
    )


def http_block(status_line="HTTP/1.1 200 OK", headers=None, body=b""):
    h = status_line.encode() + b"\r\n"
    for k, v in (headers or []):
        h += f"{k}: {v}\r\n".encode()
    return h + b"\r\n" + body


def no_cl_record(rec_id="<urn:uuid:a>", block=None, uri="http://e.com/",
                 trailing=b"\r\n\r\n"):
    """缺 Content-Length 的末记录：记录块延伸至归档 EOF。"""
    if block is None:
        block = http_block(body=b"hello") + trailing
    return (
        b"WARC/1.1\r\nWARC-Type: response\r\n"
        + f"WARC-Record-ID: {rec_id}\r\n".encode()
        + b"WARC-Date: 2020-01-01T00:00:00Z\r\n"
        + f"WARC-Target-URI: {uri}\r\n".encode()
        + b"\r\n" + block
    )


def post_pack(warc_bytes, **kw):
    return client.post(
        "/v1/member-pack",
        json={"warc": base64.b64encode(warc_bytes).decode()},
        **kw,
    )


def pack(warc_bytes):
    r = post_pack(warc_bytes)
    assert r.status_code == 200, r.text
    return r.json()


def gzip_bytes(j):
    return base64.b64decode(j["gzip_warc"])


def member_slice(gz, member):
    return gz[member["offset"]:member["offset"] + member["length"]]


def replay(warc_b64):
    r = client.post("/v1/replay", json={"warc": warc_b64})
    assert r.status_code == 200, r.text
    return r.json()["records"]


def parse_one(data):
    """解析 data 中唯一记录，返回 (记录, HTTP 头之后的原始负载字节)。

    必须在迭代过程中读取负载：迭代到下一条（或耗尽）记录时 warcio 会把
    当前记录的 raw_stream 读到边界，list 化之后再读只剩空字节。
    """
    found = []
    for rec in ArchiveIterator(io.BytesIO(data)):
        found.append((rec, rec.raw_stream.read()))
    assert len(found) == 1
    return found[0]


# ---------- 正常路径 ----------


def test_pack_basic_members_in_archive_order():
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"hello"))
    b = raw_record("<urn:uuid:b>",
                   block=http_block("HTTP/1.1 404 Not Found", body=b"nf"),
                   uri="http://b.example/")
    rev = raw_record("<urn:uuid:v>", rec_type="revisit", block=b"",
                     date="2021-06-01T00:00:00Z",
                     extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    j = pack(a + b + rev)
    assert j["count"] == 3
    assert [m["record_id"] for m in j["members"]] == [
        "<urn:uuid:a>", "<urn:uuid:b>", "<urn:uuid:v>"]

    gz = gzip_bytes(j)
    # 各 member 范围连续、互不重叠，恰好覆盖整包
    assert j["members"][0]["offset"] == 0
    ends = [m["offset"] + m["length"] for m in j["members"]]
    starts = [m["offset"] for m in j["members"]]
    assert ends[:-1] == starts[1:]
    assert ends[-1] == len(gz)

    # 每个 member 单独解压恰为一条完整 WARC 记录 + 分隔符
    for member, expected_id in zip(j["members"],
                                   ("<urn:uuid:a>", "<urn:uuid:b>",
                                    "<urn:uuid:v>")):
        dec = gzip.decompress(member_slice(gz, member))
        assert dec.startswith(b"WARC/")
        assert dec.endswith(b"\r\n\r\n")
        rec, _payload = parse_one(dec)
        assert rec.rec_headers.get_header("WARC-Record-ID") == expected_id


def test_pack_whole_decompresses_to_records_joined_by_separator():
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))
    b = raw_record("<urn:uuid:b>", block=http_block(body=b"bb"),
                   uri="http://b/")
    j = pack(a + b)
    gz = gzip_bytes(j)
    whole = gzip.decompress(gz)
    # 原记录均带 Content-Length、CRLF 行尾：解压结果与原归档逐字节一致
    assert whole == a + b
    recs = list(ArchiveIterator(io.BytesIO(whole)))
    assert [r.rec_headers.get_header("WARC-Record-ID") for r in recs] == [
        "<urn:uuid:a>", "<urn:uuid:b>"]


def test_pack_replay_equivalence():
    a = raw_record("<urn:uuid:a>",
                   block=http_block(headers=[("X-K", "v")], body=b"hello"))
    rev = raw_record("<urn:uuid:v>", rec_type="revisit", block=b"",
                     date="2021-06-01T00:00:00Z",
                     extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    warc = a + rev
    j = pack(warc)
    orig = replay(base64.b64encode(warc).decode())
    got = replay(j["gzip_warc"])
    for key in ("order", "record_id", "record_type", "http_status",
                "body_base64", "body_source_id", "target_uri", "capture_time"):
        assert [r[key] for r in got] == [r[key] for r in orig]
    assert [base64.b64decode(r["body_base64"]) for r in got] == [
        b"hello", b"hello"]
    assert [r["body_source_id"] for r in got] == [
        "<urn:uuid:a>", "<urn:uuid:a>"]


def test_pack_only_includes_response_and_revisit():
    warcinfo = (
        b"WARC/1.1\r\nWARC-Type: warcinfo\r\n"
        b"WARC-Record-ID: <urn:uuid:w>\r\nContent-Length: 0\r\n\r\n\r\n\r\n")
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"a"))
    request = (
        b"WARC/1.1\r\nWARC-Type: request\r\n"
        b"WARC-Record-ID: <urn:uuid:q>\r\n"
        b"WARC-Target-URI: http://example.com/\r\nContent-Length: 0\r\n"
        b"Content-Type: application/http; msgtype=request\r\n\r\n\r\n\r\n")
    j = pack(warcinfo + a + request)
    assert j["count"] == 1
    assert [m["record_id"] for m in j["members"]] == ["<urn:uuid:a>"]
    gz = gzip_bytes(j)
    whole = gzip.decompress(gz)
    assert b"warcinfo" not in whole and b"<urn:uuid:q>" not in whole


def test_pack_preserves_http_headers_and_payload_bytes():
    body = b"\x00\x01binary\xff\xfe"
    block = http_block("HTTP/1.1 200 OK",
                       [("X-Custom", "keep-me"),
                        ("Content-Type", "application/octet-stream")],
                       body=body)
    j = pack(raw_record("<urn:uuid:a>", block=block))
    gz = gzip_bytes(j)
    dec = gzip.decompress(member_slice(gz, j["members"][0]))
    # 头与负载逐字节保留（member = 原记录字节 + 分隔符）
    assert dec == raw_record("<urn:uuid:a>", block=block)
    rec, payload = parse_one(dec)
    assert rec.http_headers.get_header("X-Custom") == "keep-me"
    assert rec.http_headers.get_header("Content-Type") == \
        "application/octet-stream"
    # raw_stream 在 HTTP 头之后给出原始负载字节
    assert payload == body
    # 通过 warcio 正文读取也得到原负载
    recs = replay(j["gzip_warc"])
    assert base64.b64decode(recs[0]["body_base64"]) == body


# ---------- 确定性 ----------


def test_pack_deterministic_fixed_gzip_timestamp():
    warc = raw_record("<urn:uuid:a>", block=http_block(body=b"hello"))
    j1 = pack(warc)
    j2 = pack(warc)
    assert j1["gzip_warc"] == j2["gzip_warc"]
    assert j1["members"] == j2["members"]

    gz = gzip_bytes(j1)
    # gzip member 头：魔数 1f 8b，deflate CM=8，FLG=0，MTIME 固定为 0
    assert gz[:2] == b"\x1f\x8b"
    assert gz[2] == 8 and gz[3] == 0
    assert struct.unpack("<I", gz[4:8])[0] == 0


def test_pack_gzip_input_produces_same_output():
    warc = raw_record("<urn:uuid:a>", block=http_block(body=b"hello"))
    j_plain = pack(warc)
    j_gz = pack(gzip.compress(warc, compresslevel=6, mtime=1234567890))
    assert j_gz["gzip_warc"] == j_plain["gzip_warc"]


# ---------- 缺 Content-Length 的末记录 ----------


def test_pack_last_record_without_content_length_injected():
    warc = no_cl_record()  # 块延伸至 EOF（含原始尾部空行）
    orig = replay(base64.b64encode(warc).decode())
    j = pack(warc)
    assert j["count"] == 1
    gz = gzip_bytes(j)
    dec = gzip.decompress(member_slice(gz, j["members"][0]))
    rec, _payload = parse_one(dec)
    # 已注入准确块长，member 尾部的 CRLF CRLF 是分隔符而非正文
    assert rec.rec_headers.get_header("Content-Length") is not None
    declared = int(rec.rec_headers.get_header("Content-Length"))
    head_end = dec.index(b"\r\n\r\n") + 4
    # 块 = 声明长度；块之后只剩新追加的记录分隔符
    assert dec[head_end + declared:] == b"\r\n\r\n"
    got = replay(j["gzip_warc"])
    assert got[0]["body_base64"] == orig[0]["body_base64"]
    assert got[0]["http_status"] == 200


def test_pack_last_record_without_cl_and_no_trailing_separator():
    # 末记录连尾部空行都没有（EOF 即块结束）
    warc = (
        b"WARC/1.1\r\nWARC-Type: response\r\nWARC-Record-ID: <urn:uuid:a>\r\n"
        b"WARC-Date: 2020-01-01T00:00:00Z\r\n"
        b"WARC-Target-URI: http://e.com/\r\n\r\n"
        b"HTTP/1.1 200 OK\r\n\r\nhello")
    orig = replay(base64.b64encode(warc).decode())
    j = pack(warc)
    dec = gzip.decompress(gzip_bytes(j))
    rec, _payload = parse_one(dec)
    declared = int(rec.rec_headers.get_header("Content-Length"))
    head_end = dec.index(b"\r\n\r\n") + 4
    assert dec[head_end + declared:] == b"\r\n\r\n"  # 分隔符不进正文
    got = replay(j["gzip_warc"])
    assert got[0]["body_base64"] == orig[0]["body_base64"] == base64.b64encode(
        b"hello").decode()


# ---------- 空归档 ----------


def test_pack_empty_warc():
    r = client.post("/v1/member-pack", json={"warc": ""})
    assert r.status_code == 200
    j = r.json()
    assert j == {"count": 0, "gzip_warc": "", "members": []}


# ---------- 2 MiB 上限 ----------


def test_pack_too_large_after_unpacking_returns_422():
    # 输入解码后 ≤ 2 MiB，但每条 member 末尾的 CRLF CRLF 分隔符使整包
    # 解压后超过 2 MiB
    body = b"x" * (MAX_DECODED_BYTES - 200)
    block = (b"HTTP/1.1 200 OK\r\nContent-Length: "
             + str(len(body)).encode() + b"\r\n\r\n" + body)
    warc = (
        b"WARC/1.1\r\nWARC-Type: response\r\n"
        b"WARC-Record-ID: <urn:uuid:big>\r\n"
        b"WARC-Date: 2020-01-01T00:00:00Z\r\n"
        b"WARC-Target-URI: http://e.com/\r\n"
        b"Content-Length: " + str(len(block)).encode() + b"\r\n\r\n" + block)
    assert len(warc) <= MAX_DECODED_BYTES
    r = post_pack(warc)
    assert r.status_code == 422
    detail = r.json()
    assert detail["error"] == "payload_too_large"
    assert detail["decoded_size"] > MAX_DECODED_BYTES
    assert detail["max_bytes"] == MAX_DECODED_BYTES


def test_pack_input_over_2mib_returns_422():
    body = b"x" * (MAX_DECODED_BYTES + 10)
    block = (b"HTTP/1.1 200 OK\r\nContent-Length: "
             + str(len(body)).encode() + b"\r\n\r\n" + body)
    warc = (
        b"WARC/1.1\r\nWARC-Type: response\r\n"
        b"WARC-Record-ID: <urn:uuid:big>\r\n"
        b"WARC-Date: 2020-01-01T00:00:00Z\r\n"
        b"WARC-Target-URI: http://e.com/\r\n"
        b"Content-Length: " + str(len(block)).encode() + b"\r\n\r\n" + block)
    r = post_pack(warc)
    assert r.status_code == 422
    assert r.json()["error"] == "payload_too_large"


# ---------- 输入结构 / 引用校验沿用 replay 口径 ----------


def test_pack_invalid_base64():
    r = client.post("/v1/member-pack", json={"warc": "@@not-base64@@"})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_base64"


def test_pack_malformed_warc():
    r = post_pack(b"this is not a warc at all")
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"


def test_pack_missing_record_id():
    warc = (
        b"WARC/1.1\r\nWARC-Type: response\r\n"
        b"WARC-Date: 2020-01-01T00:00:00Z\r\n"
        b"WARC-Target-URI: http://e.com/\r\nContent-Length: 0\r\n\r\n\r\n\r\n")
    r = post_pack(warc)
    assert r.status_code == 422
    assert r.json()["error"] == "missing_record_id"


def test_pack_duplicate_record_id():
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"a"))
    b = raw_record("<urn:uuid:a>", block=http_block(body=b"b"),
                   uri="http://b/")
    r = post_pack(a + b)
    assert r.status_code == 422
    assert r.json()["error"] == "duplicate_record_id"


def test_pack_revisit_target_not_found():
    rev = raw_record("<urn:uuid:v>", rec_type="revisit", block=b"",
                     extra_headers=b"WARC-Refers-To: <urn:uuid:missing>\r\n")
    r = post_pack(rev)
    assert r.status_code == 422
    assert r.json()["error"] == "target_not_found"


def test_pack_reference_cycle():
    v1 = raw_record("<urn:uuid:v1>", rec_type="revisit", block=b"",
                    extra_headers=b"WARC-Refers-To: <urn:uuid:v2>\r\n")
    v2 = raw_record("<urn:uuid:v2>", rec_type="revisit", block=b"",
                    extra_headers=b"WARC-Refers-To: <urn:uuid:v1>\r\n")
    r = post_pack(v1 + v2)
    assert r.status_code == 422
    assert r.json()["error"] == "reference_cycle"


def test_pack_response_without_http_headers():
    warc = raw_record("<urn:uuid:a>", block=b"not an http response")
    r = post_pack(warc)
    assert r.status_code == 422
    assert r.json()["error"] == "response_without_http_headers"


def test_pack_error_in_unincluded_record_still_422():
    # warcinfo 虽不收录，但归档结构错误（截断末记录）仍使整包失败
    warcinfo = (
        b"WARC/1.1\r\nWARC-Type: warcinfo\r\n"
        b"WARC-Record-ID: <urn:uuid:w>\r\nContent-Length: 10\r\n\r\nshort")
    r = post_pack(warcinfo)
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"


# ---------- 请求模型 ----------


def test_pack_requires_warc_field():
    r = client.post("/v1/member-pack", json={})
    assert r.status_code == 422
