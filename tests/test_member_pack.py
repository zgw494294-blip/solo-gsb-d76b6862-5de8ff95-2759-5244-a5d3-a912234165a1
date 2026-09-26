"""POST /v1/member-pack 端到端测试：多 member gzip 打包、按 member 定位、
固定时间戳确定性、末记录缺 Content-Length 补长与整包回放等价。"""

from __future__ import annotations

import base64
import gzip
import io
import zlib

from fastapi.testclient import TestClient
from warcio.archiveiterator import WARCIterator

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


def no_cl_record(rec_id="<urn:uuid:a>", block=None,
                 uri="http://e.com/", rec_type="response",
                 extra_headers=b""):
    """缺 Content-Length 的末记录：记录块延伸至归档 EOF。"""
    if block is None:
        block = http_block(body=b"hello")
    return (
        f"WARC/1.1\r\nWARC-Type: {rec_type}\r\n"
        f"WARC-Record-ID: {rec_id}\r\n"
        f"WARC-Date: 2020-01-01T00:00:00Z\r\n"
        f"WARC-Target-URI: {uri}\r\n".encode()
        + extra_headers + b"\r\n" + block
    )


def post_pack(warc_bytes, **kw):
    return client.post(
        "/v1/member-pack",
        json={"warc": base64.b64encode(warc_bytes).decode()},
        **kw,
    )


def unpack(response):
    assert response.status_code == 200, response.text
    data = response.json()
    return data, base64.b64decode(data["gzip_warc"])


def replay_records(warc_bytes):
    r = client.post("/v1/replay",
                    json={"warc": base64.b64encode(warc_bytes).decode()})
    assert r.status_code == 200, r.text
    return r.json()["records"]


def bodies(records):
    return [base64.b64decode(rec["body_base64"]) for rec in records]


def iter_decompressed(gz_bytes):
    """按多 member gzip 顺序解压，返回每个 member 的原始字节列表。"""
    members = []
    remaining = gz_bytes
    while remaining[:2] == b"\x1f\x8b":
        d = zlib.decompressobj(31)
        members.append(d.decompress(remaining) + d.flush())
        remaining = d.unused_data
    assert not remaining.strip()
    return members


# ---------- 正常路径 ----------


def test_pack_basic_one_gzip_member_per_record():
    a = raw_record("<urn:uuid:a>", block=http_block(
        headers=[("X-Custom", "v1")], body=b"hello"))
    rev = raw_record("<urn:uuid:rev>", rec_type="revisit", block=b"",
                     date="2021-06-01T00:00:00Z",
                     extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    b = raw_record("<urn:uuid:b>", block=http_block(
        "HTTP/1.1 404 Not Found", body=b"nf"), uri="http://b.example/")
    data, gz = unpack(post_pack(a + rev + b))

    assert data["count"] == 3
    assert [m["record_id"] for m in data["members"]] == [
        "<urn:uuid:a>", "<urn:uuid:rev>", "<urn:uuid:b>"]

    # 每个 member 单独解压：完整 WARC 记录（自版本行起）+ 记录分隔符
    members = iter_decompressed(gz)
    assert len(members) == 3
    for payload in members:
        assert payload.startswith(b"WARC/1.1\r\n")
        assert payload.endswith(b"\r\n\r\n")
        # 单 member 本身可被 warcio 解析为一条记录
        recs = list(WARCIterator(io.BytesIO(payload)))
        assert len(recs) == 1

    # 整包解压结果交给 /v1/replay：顺序、状态、正文、来源 ID 与输入一致
    packed = gzip.decompress(gz)
    original = replay_records(a + rev + b)
    repacked = replay_records(packed)
    assert repacked == original
    assert bodies(repacked) == [b"hello", b"hello", b"nf"]
    assert [r["http_status"] for r in repacked] == [200, None, 404]
    assert [r["body_source_id"] for r in repacked] == [
        "<urn:uuid:a>", "<urn:uuid:a>", "<urn:uuid:b>"]


def test_pack_members_offsets_are_contiguous_and_non_overlapping():
    records = (
        raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))
        + raw_record("<urn:uuid:b>", block=http_block(body=b"bb"),
                     uri="http://b.example/")
        + raw_record("<urn:uuid:c>", block=http_block(body=b"cc"),
                     uri="http://c.example/")
    )
    data, gz = unpack(post_pack(records))
    members = data["members"]

    assert members[0]["offset"] == 0
    for prev, cur in zip(members, members[1:]):
        assert cur["offset"] == prev["offset"] + prev["length"]
    assert members[-1]["offset"] + members[-1]["length"] == len(gz)

    # 每个压缩范围切出后都是合法、可独立解压且只含本记录的 gzip member
    for m in members:
        one = gz[m["offset"]:m["offset"] + m["length"]]
        out = gzip.decompress(one)
        rid = f"WARC-Record-ID: {m['record_id']}\r\n".encode()
        assert rid in out
        recs = list(WARCIterator(io.BytesIO(out)))
        assert len(recs) == 1
        assert recs[0].rec_headers.get_header(
            "WARC-Record-ID") == m["record_id"]


def test_pack_preserves_http_headers_and_payload_bytes():
    block = http_block("HTTP/1.1 200 OK",
                       [("X-One", "1"), ("X-Two", "two")],
                       body=b"\x00\x01raw-bytes\xff")
    warc = raw_record("<urn:uuid:a>", block=block,
                      extra_headers=b"WARC-Block-Digest: sha1:ABC\r\n")
    data, gz = unpack(post_pack(warc))
    out = gzip.decompress(gz)
    # 完整 WARC 头、HTTP 头与负载逐字节保留；末尾追加一个记录分隔符
    assert out == warc[:-len(b"\r\n\r\n")] + b"\r\n\r\n"
    assert out.startswith(b"WARC/1.1\r\nWARC-Type: response\r\n")
    assert b"X-One: 1\r\nX-Two: two\r\n\r\n\x00\x01raw-bytes\xff" in out


def test_pack_skips_non_target_record_types():
    info = raw_record("<urn:uuid:info>", rec_type="warcinfo",
                      block=b"info: x\r\n")
    req = raw_record("<urn:uuid:q>", rec_type="request",
                     block=b"GET / HTTP/1.1\r\nHost: e.com\r\n\r\n",
                     extra_headers=b"Content-Type: application/http; "
                                   b"msgtype=request\r\n")
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))
    data, gz = unpack(post_pack(info + req + a))
    assert data["count"] == 1
    assert [m["record_id"] for m in data["members"]] == ["<urn:uuid:a>"]
    assert len(iter_decompressed(gz)) == 1
    assert [r["record_id"] for r in replay_records(gzip.decompress(gz))] == [
        "<urn:uuid:a>"]


def test_pack_empty_warc():
    data, gz = unpack(post_pack(b""))
    assert data["count"] == 0
    assert data["members"] == []
    assert gz == b""
    assert data["gzip_warc"] == ""
    # 空包交给 /v1/replay 得到空记录列表
    assert replay_records(gz) == []


def test_pack_accepts_gzip_input_single_and_multi_member():
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"gz-a"))
    b = raw_record("<urn:uuid:b>", block=http_block(body=b"gz-b"),
                   uri="http://b.example/")
    plain = a + b
    # 单流 gzip
    d1, gz1 = unpack(post_pack(gzip.compress(plain)))
    # 多 member gzip 输入
    multi = gzip.compress(a) + gzip.compress(b)
    d2, gz2 = unpack(post_pack(multi))
    assert d1 == d2
    assert gz1 == gz2
    assert bodies(replay_records(gzip.decompress(gz1))) == [b"gz-a", b"gz-b"]


# ---------- 确定性：固定 gzip 时间戳 ----------


def test_pack_deterministic_same_bytes_for_same_input():
    warc = (raw_record("<urn:uuid:a>", block=http_block(body=b"hello"))
            + raw_record("<urn:uuid:rev>", rec_type="revisit", block=b"",
                         date="2021-06-01T00:00:00Z",
                         extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n"))
    r1 = post_pack(warc)
    r2 = post_pack(warc)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    gz = base64.b64decode(r1.json()["gzip_warc"])
    # gzip 头：魔数、压缩方法 8、FLG=0；MTIME（偏移 4..8）固定为 0
    assert gz[0:2] == b"\x1f\x8b"
    assert gz[2] == 8 and gz[3] == 0
    assert gz[4:8] == b"\x00\x00\x00\x00"


def test_pack_each_member_has_zero_mtime():
    warc = (raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))
            + raw_record("<urn:uuid:b>", block=http_block(body=b"bb"),
                         uri="http://b.example/"))
    data, gz = unpack(post_pack(warc))
    for m in data["members"]:
        head = gz[m["offset"]:m["offset"] + 10]
        assert head[0:4] == b"\x1f\x8b\x08\x00"
        assert head[4:8] == b"\x00\x00\x00\x00"


# ---------- 末记录缺 Content-Length ----------


def test_pack_last_record_without_content_length_gets_accurate_length():
    # 末记录缺 Content-Length，块后还跟着记录间空行（span 延伸至 EOF）
    http = http_block(body=b"hello")
    warc = no_cl_record("<urn:uuid:a>", block=http + b"\r\n\r\n")
    original = replay_records(warc)

    data, gz = unpack(post_pack(warc))
    payload = gzip.decompress(gz)
    # 补入准确块长：span 尾部的记录间空行保留在块内（warcio 解析正文时把
    # 块内尾对与块外分隔符配对为边界），故块长 = HTTP 头/正文 + 一对空行；
    # member 块外再追加一个分隔符
    block_with_trailing_sep = http + b"\r\n\r\n"
    assert (f"Content-Length: {len(block_with_trailing_sep)}\r\n".encode()
            in payload)
    recs = list(WARCIterator(io.BytesIO(payload)))
    assert len(recs) == 1
    rec = recs[0]
    declared = int(rec.rec_headers.get_header("Content-Length"))
    rec.raw_stream.read()
    assert rec.raw_stream.tell() == declared
    assert declared == len(block_with_trailing_sep)

    repacked = replay_records(payload)
    assert repacked[0]["body_base64"] == original[0]["body_base64"]
    assert bodies(repacked) == [b"hello\r\n\r\n"]


def test_pack_no_cl_last_record_separator_not_in_body():
    # 末记录块本身不以换行结尾：补长后分隔符绝不能进入正文
    warc = no_cl_record("<urn:uuid:a>", block=http_block(body=b"world"))
    data, gz = unpack(post_pack(warc))
    recs = replay_records(gzip.decompress(gz))
    assert bodies(recs) == [b"world"]


def test_pack_no_cl_last_after_normal_records():
    first = raw_record("<urn:uuid:f>", block=http_block(body=b"first"),
                       uri="http://f.example/")
    last = no_cl_record("<urn:uuid:a>")
    warc = first + last
    data, gz = unpack(post_pack(warc))
    assert data["count"] == 2
    members = iter_decompressed(gz)
    # 首记录 member 字节与原记录（含其尾部分隔符）一致
    assert members[0] == first
    recs = replay_records(gzip.decompress(gz))
    original = replay_records(warc)
    assert [r["body_base64"] for r in recs] == [
        r["body_base64"] for r in original]
    assert bodies(recs) == [b"first", b"hello"]


def test_pack_no_cl_revisit_record():
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"hello"))
    rev = no_cl_record(
        "<urn:uuid:v>", block=b"", rec_type="revisit",
        uri="http://v.example/",
        extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    # 原始输入：revisit 块为空（头后 EOF），其“块”即 0 字节
    data, gz = unpack(post_pack(a + rev))
    payload = gzip.decompress(gz)
    recs = replay_records(payload)
    assert [r["record_id"] for r in recs] == ["<urn:uuid:a>", "<urn:uuid:v>"]
    assert bodies(recs) == [b"hello", b"hello"]
    assert recs[1]["body_source_id"] == "<urn:uuid:a>"
    # revisit member 补了 Content-Length: 0
    assert b"Content-Length: 0\r\n" in iter_decompressed(gz)[1]


def test_pack_no_cl_last_lf_line_endings():
    # 纯 LF 行尾的缺长末记录（头后块直接到 EOF）
    warc = (b"WARC/1.1\nWARC-Type: response\nWARC-Record-ID: <urn:uuid:a>\n"
            b"WARC-Date: 2020-01-01T00:00:00Z\nWARC-Target-URI: http://e/\n\n"
            b"HTTP/1.1 200 OK\n\nhello")
    original = replay_records(warc)
    data, gz = unpack(post_pack(warc))
    payload = gzip.decompress(gz)
    # 补长行沿用 LF 结束行风格；member 末尾统一为 CRLF CRLF 分隔符
    assert b"Content-Length: 22\n\n" in payload
    assert payload.endswith(b"\r\n\r\n")
    repacked = replay_records(payload)
    assert repacked[0]["body_base64"] == original[0]["body_base64"]
    assert bodies(repacked) == [b"hello"]


def test_pack_no_cl_last_lf_with_trailing_blank_lines():
    # LF 行尾、块后还跟着记录间空行（span 延伸至 EOF）
    warc = (b"WARC/1.1\nWARC-Type: response\nWARC-Record-ID: <urn:uuid:a>\n"
            b"WARC-Date: 2020-01-01T00:00:00Z\nWARC-Target-URI: http://e/\n\n"
            b"HTTP/1.1 200 OK\n\nhello\n\n")
    original = replay_records(warc)
    data, gz = unpack(post_pack(warc))
    payload = gzip.decompress(gz)
    repacked = replay_records(payload)
    # 原包正文含尾部空行（warcio 无长度块到 EOF 的口径），打包后逐字节一致
    assert repacked[0]["body_base64"] == original[0]["body_base64"]
    assert bodies(repacked) == [b"hello\n\n"]


# ---------- 校验沿用：结构 / revisit 引用 ----------


def test_pack_invalid_base64_422():
    r = client.post("/v1/member-pack", json={"warc": "!!!not-base64!!!"})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_base64"


def test_pack_input_too_large_422():
    blob = b"x" * (MAX_DECODED_BYTES + 1)
    r = client.post(
        "/v1/member-pack",
        json={"warc": base64.b64encode(blob).decode()})
    assert r.status_code == 422
    assert r.json()["error"] == "payload_too_large"


def test_pack_malformed_warc_422():
    r = post_pack(b"this is not a warc file at all")
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"


def test_pack_truncated_block_422():
    # 末记录头完整、Content-Length 为正但块缺失：截断 → 422
    warc = (b"WARC/1.1\r\nWARC-Type: response\r\n"
            b"WARC-Record-ID: <urn:uuid:a>\r\n"
            b"WARC-Date: 2020-01-01T00:00:00Z\r\n"
            b"WARC-Target-URI: http://e.com/\r\n"
            b"Content-Length: 100\r\n\r\nshort")
    r = post_pack(warc)
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"


def test_pack_broken_revisit_chain_422():
    rev = raw_record(
        "<urn:uuid:v>", rec_type="revisit", block=b"",
        extra_headers=b"WARC-Refers-To: <urn:uuid:missing>\r\n")
    r = post_pack(rev)
    assert r.status_code == 422
    assert r.json()["error"] == "target_not_found"


def test_pack_duplicate_record_id_422():
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))
    dup = a + raw_record("<urn:uuid:a>", block=http_block(body=b"cc"),
                         uri="http://c.example/")
    r = post_pack(dup)
    assert r.status_code == 422
    assert r.json()["error"] == "duplicate_record_id"


def test_pack_response_without_http_headers_422():
    bad = raw_record("<urn:uuid:a>", block=b"not an http response")
    r = post_pack(bad)
    assert r.status_code == 422
    assert r.json()["error"] == "response_without_http_headers"


# ---------- 输出（解压后）体积上限 ----------


def test_pack_packed_exceeds_limit_422():
    # 输入 ≤ 2 MiB；末记录缺 Content-Length，打包后必追加记录分隔符，
    # 令解压后结果恰好超出上限 2 字节 → 422
    head = (b"WARC/1.1\r\nWARC-Type: response\r\n"
            b"WARC-Record-ID: <urn:uuid:a>\r\n"
            b"WARC-Date: 2020-01-01T00:00:00Z\r\n"
            b"WARC-Target-URI: http://e.com/\r\n\r\n")
    http_head = b"HTTP/1.1 200 OK\r\n\r\n"
    body_len = MAX_DECODED_BYTES - len(head) - len(http_head) - 2
    warc = head + http_head + b"x" * body_len
    assert len(warc) == MAX_DECODED_BYTES - 2
    r = post_pack(warc)
    assert r.status_code == 422
    data = r.json()
    assert data["error"] == "payload_too_large"
    assert data["decoded_size"] > MAX_DECODED_BYTES
    assert data["max_bytes"] == MAX_DECODED_BYTES


def test_pack_packed_under_limit_ok():
    # 同一构造，输入再短一些使“打包后（含补长行与分隔符）”仍 ≤ 2 MiB
    head = (b"WARC/1.1\r\nWARC-Type: response\r\n"
            b"WARC-Record-ID: <urn:uuid:a>\r\n"
            b"WARC-Date: 2020-01-01T00:00:00Z\r\n"
            b"WARC-Target-URI: http://e.com/\r\n\r\n")
    http_head = b"HTTP/1.1 200 OK\r\n\r\n"
    body_len = MAX_DECODED_BYTES - len(head) - len(http_head) - 200
    warc = head + http_head + b"x" * body_len
    assert len(warc) <= MAX_DECODED_BYTES
    data, gz = unpack(post_pack(warc))
    assert len(gzip.decompress(gz)) <= MAX_DECODED_BYTES
    assert data["count"] == 1


# ---------- 整包多 member 解压后回放等价 ----------


def test_pack_multi_member_round_trip_with_warcio_stream():
    a = raw_record("<urn:uuid:a>", block=http_block(
        "HTTP/1.1 200 OK", [("X-A", "1")], body=b"aaa"))
    rev = raw_record("<urn:uuid:rev>", rec_type="revisit", block=b"",
                     date="2021-06-01T00:00:00Z",
                     extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    c = raw_record("<urn:uuid:c>", block=http_block(
        "HTTP/1.1 301 Moved", [("Location", "http://e.com/new")],
        body=b"go"), uri="http://e.com/old")
    data, gz = unpack(post_pack(a + rev + c))

    # warcio 直接从多 member gzip 流顺序读出三条记录（raw_stream 已消费
    # HTTP 头，读出的是块内 HTTP 正文原字节）
    seen = []
    for rec in WARCIterator(io.BytesIO(gz)):
        seen.append((
            rec.rec_headers.get_header("WARC-Record-ID"),
            rec.rec_type,
            rec.http_headers,
            rec.raw_stream.read(),
        ))
    assert [x[0] for x in seen] == [
        "<urn:uuid:a>", "<urn:uuid:rev>", "<urn:uuid:c>"]
    assert [x[1] for x in seen] == ["response", "revisit", "response"]
    # HTTP 响应头原字节保留：状态行与自定义头都在
    assert seen[0][2].get_statuscode() == "200"
    assert seen[0][2].get_header("X-A") == "1"
    assert seen[2][2].get_statuscode() == "301"
    assert seen[2][2].get_header("Location") == "http://e.com/new"
    # 块内正文原字节保留（revisit 块为空，正文经引用链在 /v1/replay 还原）
    assert seen[0][3] == b"aaa"
    assert seen[1][3] == b""
    assert seen[2][3] == b"go"


def test_pack_multilevel_revisit_chain_round_trip():
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"base"))
    v2 = raw_record(
        "<urn:uuid:v2>", rec_type="revisit", block=b"",
        date="2021-02-02T00:00:00Z",
        extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    v1 = raw_record(
        "<urn:uuid:v1>", rec_type="revisit", block=b"",
        date="2021-01-01T00:00:00Z",
        extra_headers=b"WARC-Refers-To: <urn:uuid:v2>\r\n")
    warc = a + v2 + v1
    data, gz = unpack(post_pack(warc))
    recs = replay_records(gzip.decompress(gz))
    assert [r["record_id"] for r in recs] == [
        "<urn:uuid:a>", "<urn:uuid:v2>", "<urn:uuid:v1>"]
    assert bodies(recs) == [b"base", b"base", b"base"]
    assert [r["body_source_id"] for r in recs] == [
        "<urn:uuid:a>", "<urn:uuid:a>", "<urn:uuid:a>"]


def test_pack_member_count_matches_replay_count():
    warc = (raw_record("<urn:uuid:a>", block=http_block(body=b"a"))
            + raw_record("<urn:uuid:b>", block=http_block(body=b"b"),
                         uri="http://b.example/"))
    data, gz = unpack(post_pack(warc))
    assert data["count"] == len(data["members"])
    assert data["count"] == len(replay_records(warc))
