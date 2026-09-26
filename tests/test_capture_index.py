"""POST /v1/capture-index 端到端测试。

覆盖：排序键（URL 原值 / UTC 时刻 / 归档序号）、同刻记录全保留、
offset/length 字节切片还原、缺 URL/日期跳过、非法日期 422、revisit 字段、
分页与 cursor 绑定（跨包/损坏/陈旧）、page_size 边界、gzip、其余 WARC
校验沿用现有规则。
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json

from fastapi.testclient import TestClient
from warcio.archiveiterator import ArchiveIterator

from app.main import app
from app.replay import MAX_DECODED_BYTES

client = TestClient(app)


# ---------- WARC 构造辅助（与 test_extract.py 同风格） ----------


def raw_record(rec_id="<urn:uuid:r>", rec_type="response", block=b"",
               extra_headers=b"", date="2020-01-01T00:00:00Z",
               uri="http://example.com/", with_uri=True, with_date=True):
    head = (
        f"WARC/1.1\r\nWARC-Type: {rec_type}\r\n"
        f"WARC-Record-ID: {rec_id}\r\n"
    ).encode()
    if with_date:
        head += f"WARC-Date: {date}\r\n".encode()
    if with_uri:
        head += f"WARC-Target-URI: {uri}\r\n".encode()
    head += f"Content-Length: {len(block)}\r\n".encode()
    return head + extra_headers + b"\r\n" + block + b"\r\n\r\n"


def http_block(status_line="HTTP/1.1 200 OK", headers=None, body=b""):
    h = status_line.encode() + b"\r\n"
    for k, v in (headers or []):
        h += f"{k}: {v}\r\n".encode()
    return h + b"\r\n" + body


def post_index(warc_bytes, page_size=100, cursor=None, **kw):
    payload = {
        "warc": base64.b64encode(warc_bytes).decode(),
        "page_size": page_size,
    }
    if cursor is not None:
        payload["cursor"] = cursor
    return client.post("/v1/capture-index", json=payload, **kw)


def encode_cursor(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


# ---------- 排序与字段 ----------


def test_sorted_by_url_moment_archive_order():
    # 归档顺序故意打乱；期望按 (URL 原值, UTC 时刻, 归档序号) 升序
    r1 = raw_record("<urn:uuid:1>", uri="http://b.example/",
                    date="2021-01-01T00:00:00Z",
                    block=http_block(body=b"b1"))
    r2 = raw_record("<urn:uuid:2>", uri="http://a.example/",
                    date="2021-06-01T00:00:00Z",
                    block=http_block(body=b"a2"))
    r3 = raw_record("<urn:uuid:3>", uri="http://b.example/",
                    date="2020-01-01T00:00:00Z",
                    block=http_block(body=b"b0"))
    r4 = raw_record("<urn:uuid:4>", uri="http://a.example/",
                    date="2020-01-01T00:00:00Z",
                    block=http_block(body=b"a0"))
    r = post_index(r1 + r2 + r3 + r4)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] == 4
    assert data["next_cursor"] is None
    assert [(e["record_id"], e["capture_time"]) for e in data["entries"]] == [
        ("<urn:uuid:4>", "2020-01-01T00:00:00Z"),
        ("<urn:uuid:2>", "2021-06-01T00:00:00Z"),
        ("<urn:uuid:3>", "2020-01-01T00:00:00Z"),
        ("<urn:uuid:1>", "2021-01-01T00:00:00Z"),
    ]


def test_same_url_same_utc_instant_all_kept_in_archive_order():
    # Z / +01:00 / -05:00 三种写法对应同一 UTC 时刻：全部保留，按归档序号
    r1 = raw_record("<urn:uuid:z>", date="2020-01-01T00:00:00Z",
                    block=http_block(body=b"z"))
    r2 = raw_record("<urn:uuid:p1>", date="2020-01-01T01:00:00+01:00",
                    block=http_block(body=b"p"))
    r3 = raw_record("<urn:uuid:m>", date="2019-12-31T19:00:00-05:00",
                    block=http_block(body=b"m"))
    r = post_index(r1 + r2 + r3)
    assert r.status_code == 200, r.text
    assert [e["record_id"] for e in r.json()["entries"]] == [
        "<urn:uuid:z>", "<urn:uuid:p1>", "<urn:uuid:m>"]


def test_url_compared_by_raw_value_not_normalized():
    # 大小写/结尾斜杠不同即不同 URL，按原值字典序
    r1 = raw_record("<urn:uuid:1>", uri="http://Example.com/",
                    block=http_block(body=b"1"))
    r2 = raw_record("<urn:uuid:2>", uri="http://example.com",
                    block=http_block(body=b"2"))
    r3 = raw_record("<urn:uuid:3>", uri="http://example.com/",
                    block=http_block(body=b"3"))
    r = post_index(r1 + r2 + r3)
    assert r.status_code == 200, r.text
    # 'E' < 'e'（码点序）；"http://example.com" 是 "http://example.com/" 的前缀
    assert [(e["record_id"], e["capture_time"]) for e in r.json()["entries"]] == [
        ("<urn:uuid:1>", "2020-01-01T00:00:00Z"),
        ("<urn:uuid:2>", "2020-01-01T00:00:00Z"),
        ("<urn:uuid:3>", "2020-01-01T00:00:00Z"),
    ]


def test_entry_fields_exact():
    a = raw_record(
        "<urn:uuid:a>",
        block=http_block("HTTP/1.1 201 Created", body=b"x"),
        extra_headers=b"Content-Type: application/http; msgtype=response\r\n")
    r = post_index(a)
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"total", "entries", "next_cursor"}
    entry = r.json()["entries"][0]
    assert set(entry) == {
        "record_id", "record_type", "capture_time", "http_status",
        "body_source_id", "offset", "length",
    }
    assert entry["record_id"] == "<urn:uuid:a>"
    assert entry["record_type"] == "response"
    assert entry["capture_time"] == "2020-01-01T00:00:00Z"
    assert entry["http_status"] == 201
    assert entry["body_source_id"] == "<urn:uuid:a>"
    assert entry["offset"] == 0
    assert entry["length"] == len(a) - len(b"\r\n\r\n")


def test_revisit_body_source_and_own_status():
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"ORIGINAL"))
    rev = raw_record(
        "<urn:uuid:rev>", rec_type="revisit",
        block=http_block("HTTP/1.1 304 Not Modified", body=b""),
        date="2021-06-01T00:00:00Z",
        extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    rev_bare = raw_record(
        "<urn:uuid:rev2>", rec_type="revisit", block=b"",
        date="2022-01-01T00:00:00Z",
        extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    r = post_index(a + rev + rev_bare)
    assert r.status_code == 200, r.text
    by_id = {e["record_id"]: e for e in r.json()["entries"]}
    assert by_id["<urn:uuid:a>"]["http_status"] == 200
    assert by_id["<urn:uuid:a>"]["body_source_id"] == "<urn:uuid:a>"
    assert by_id["<urn:uuid:rev>"]["http_status"] == 304
    assert by_id["<urn:uuid:rev>"]["body_source_id"] == "<urn:uuid:a>"
    assert by_id["<urn:uuid:rev>"]["record_type"] == "revisit"
    # revisit 自身无 HTTP 头 → http_status 为 null，正文仍来自终点 response
    assert by_id["<urn:uuid:rev2>"]["http_status"] is None
    assert by_id["<urn:uuid:rev2>"]["body_source_id"] == "<urn:uuid:a>"


# ---------- offset / length 字节范围 ----------


def test_offset_length_slice_restores_original_record():
    a = raw_record(
        "<urn:uuid:a>", block=http_block(body=b"hello"),
        extra_headers=b"Content-Type: application/http; msgtype=response\r\n"
                      b"X-Custom: kept\r\n")
    b = raw_record(
        "<urn:uuid:b>", rec_type="revisit", block=b"",
        date="2021-01-01T00:00:00Z",
        extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    archive = a + b
    r = post_index(archive)
    assert r.status_code == 200, r.text
    entries = r.json()["entries"]  # 同 URL：a(2020) 在前、b(2021) 在后
    for entry, rec_bytes in zip(entries, (a, b)):
        off, ln = entry["offset"], entry["length"]
        slice_bytes = archive[off:off + ln]
        # 范围自 WARC 版本行起、不含尾部分隔符
        assert slice_bytes == rec_bytes[: -len(b"\r\n\r\n")]
        assert slice_bytes.startswith(b"WARC/1.1\r\n")
        # 切片自身仍可作为一条完整记录解析，记录 ID 与 HTTP 状态不变
        found = list(ArchiveIterator(io.BytesIO(slice_bytes)))
        assert len(found) == 1
        assert found[0].rec_headers.get_header("WARC-Record-ID") == \
            entry["record_id"]
        found[0].raw_stream.read()


def test_offset_skips_non_target_records():
    warcinfo = (
        b"WARC/1.1\r\nWARC-Type: warcinfo\r\n"
        b"WARC-Record-ID: <urn:uuid:info>\r\n"
        b"WARC-Date: 2020-01-01T00:00:00Z\r\nContent-Length: 4\r\n\r\n"
        b"info\r\n\r\n"
    )
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"x"))
    r = post_index(warcinfo + a)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] == 1
    entry = data["entries"][0]
    assert entry["record_id"] == "<urn:uuid:a>"
    # warcinfo 不计入列出但仍占归档字节；偏移从该记录自己的版本行起算
    assert entry["offset"] == len(warcinfo)
    assert (warcinfo + a)[entry["offset"]:
                          entry["offset"] + entry["length"]] == a[:-4]


def test_gzip_offsets_relative_to_decompressed_archive():
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"hello"))
    b = raw_record("<urn:uuid:b>", uri="http://z.example/",
                   block=http_block(body=b"world"))
    plain = a + b
    r = post_index(gzip.compress(a) + gzip.compress(b))  # 多 member gzip
    assert r.status_code == 200, r.text
    entries = r.json()["entries"]
    assert [e["record_id"] for e in entries] == [
        "<urn:uuid:a>", "<urn:uuid:b>"]
    assert entries[0]["offset"] == 0
    for entry, rec_bytes in zip(entries, (a, b)):
        assert plain[entry["offset"]:
                     entry["offset"] + entry["length"]] == rec_bytes[:-4]


# ---------- 缺字段与非法日期 ----------


def test_missing_url_or_date_records_skipped():
    good = raw_record("<urn:uuid:good>", block=http_block(body=b"g"))
    no_uri = raw_record("<urn:uuid:no-uri>", block=http_block(body=b"x"),
                        with_uri=False)
    no_date = raw_record("<urn:uuid:no-date>",
                         uri="http://other.example/",
                         block=http_block(body=b"y"), with_date=False)
    r = post_index(good + no_uri + no_date)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] == 1
    assert [e["record_id"] for e in data["entries"]] == ["<urn:uuid:good>"]


def test_invalid_capture_date_returns_422():
    bad = raw_record("<urn:uuid:bad>", date="not-a-date",
                     block=http_block(body=b"x"))
    r = post_index(bad)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "invalid_capture_date"
    assert d["record_id"] == "<urn:uuid:bad>"
    assert d["record_order"] == 0
    assert d["warc_date"] == "not-a-date"


def test_invalid_date_on_record_without_url_is_skipped_not_422():
    # 缺 URL 的记录整体跳过，其日期非法也不连累整包（与 navigate 一致）
    good = raw_record("<urn:uuid:good>", block=http_block(body=b"g"))
    bad = raw_record("<urn:uuid:bad>", date="garbage", with_uri=False,
                     block=http_block(body=b"x"))
    r = post_index(good + bad)
    assert r.status_code == 200, r.text
    assert [e["record_id"] for e in r.json()["entries"]] == ["<urn:uuid:good>"]


def test_empty_archive():
    r = post_index(b"", page_size=10)
    assert r.status_code == 200
    assert r.json() == {"total": 0, "entries": [], "next_cursor": None}


# ---------- 分页与 cursor ----------


def test_pagination_walk_covers_all_entries():
    recs = [
        raw_record(f"<urn:uuid:{i}>", uri=f"http://e{i}.example/",
                   block=http_block(body=f"b{i}".encode()))
        for i in range(7)
    ]
    archive = b"".join(recs)
    seen, cursor, pages = [], None, 0
    while True:
        r = post_index(archive, page_size=3, cursor=cursor)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["total"] == 7
        seen.extend(e["record_id"] for e in data["entries"])
        pages += 1
        cursor = data["next_cursor"]
        if cursor is None:
            break
        assert pages < 10
    assert pages == 3  # 3 + 3 + 1
    assert seen == [f"<urn:uuid:{i}>" for i in range(7)]


def test_first_page_includes_cursor_when_more_remain():
    recs = b"".join(
        raw_record(f"<urn:uuid:{i}>", uri=f"http://e{i}.example/",
                   block=http_block(body=b"x"))
        for i in range(3))
    r = post_index(recs, page_size=2)
    assert r.status_code == 200, r.text
    data = r.json()
    assert [e["record_id"] for e in data["entries"]] == [
        "<urn:uuid:0>", "<urn:uuid:1>"]
    assert data["next_cursor"] is not None


def test_last_page_cursor_is_null_even_with_cursor_input():
    recs = b"".join(
        raw_record(f"<urn:uuid:{i}>", uri=f"http://e{i}.example/",
                   block=http_block(body=b"x"))
        for i in range(3))
    cursor = post_index(recs, page_size=2).json()["next_cursor"]
    r = post_index(recs, page_size=2, cursor=cursor)
    data = r.json()
    assert [e["record_id"] for e in data["entries"]] == ["<urn:uuid:2>"]
    assert data["next_cursor"] is None


def test_cursor_independent_of_page_size():
    recs = b"".join(
        raw_record(f"<urn:uuid:{i}>", uri=f"http://e{i}.example/",
                   block=http_block(body=b"x"))
        for i in range(5))
    cursor = post_index(recs, page_size=2).json()["next_cursor"]
    # 翻页时放大 page_size：cursor 只绑定归档与排序键
    r = post_index(recs, page_size=100, cursor=cursor)
    assert r.status_code == 200, r.text
    data = r.json()
    assert [e["record_id"] for e in data["entries"]] == [
        "<urn:uuid:2>", "<urn:uuid:3>", "<urn:uuid:4>"]
    assert data["next_cursor"] is None


def test_cursor_pointing_past_last_entry_yields_empty_page():
    # 正常流程只会给指向非末条的 cursor；手工构造指向末条的 cursor，
    # 应得到空页且 next_cursor 为 null（而非报错）
    recs = b"".join(
        raw_record(f"<urn:uuid:{i}>", uri=f"http://e{i}.example/",
                   block=http_block(body=b"x"))
        for i in range(3))
    cursor = encode_cursor({
        "v": 1,
        "sha": hashlib.sha256(recs).hexdigest(),
        "url": "http://e2.example/",
        "ts": "2020-01-01T00:00:00Z",
        "order": 2,
    })
    r = post_index(recs, page_size=10, cursor=cursor)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] == 3
    assert data["entries"] == []
    assert data["next_cursor"] is None


def test_cursor_from_other_archive_returns_422():
    a = raw_record("<urn:uuid:a1>", block=http_block(body=b"1")) + \
        raw_record("<urn:uuid:a2>", block=http_block(body=b"2"))
    b = raw_record("<urn:uuid:b1>", uri="http://b.example/",
                   block=http_block(body=b"1")) + \
        raw_record("<urn:uuid:a2>", block=http_block(body=b"2"))
    cursor = post_index(a, page_size=1).json()["next_cursor"]
    r = post_index(b, page_size=1, cursor=cursor)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "invalid_cursor"
    assert d["reason"] == "archive_mismatch"


def test_stale_cursor_same_archive_but_unknown_sort_key_422():
    recs = raw_record("<urn:uuid:a>", block=http_block(body=b"x"))
    cursor = encode_cursor({
        "v": 1,
        "sha": hashlib.sha256(recs).hexdigest(),
        "url": "http://example.com/",
        "ts": "2020-01-01T00:00:00Z",
        "order": 99,  # 同包但不存在的归档序号
    })
    r = post_index(recs, page_size=1, cursor=cursor)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "invalid_cursor"
    assert d["reason"] == "stale_cursor"


def test_malformed_cursors_return_422():
    recs = raw_record("<urn:uuid:a>", block=http_block(body=b"x"))
    bad_cursors = [
        "",
        "!!!not-base64!!!",
        base64.b64encode(b"hello").decode(),           # 非 JSON
        base64.b64encode(b"{}").decode(),              # 缺字段
        encode_cursor({"v": 99, "sha": "s", "url": "u",  # 版本不支持
                       "ts": "2020-01-01T00:00:00Z", "order": 0}),
        "not ascii é",
    ]
    for bad in bad_cursors:
        r = post_index(recs, page_size=1, cursor=bad)
        assert r.status_code == 422, bad
        assert r.json()["error"] == "invalid_cursor", bad


def test_cursor_with_wrong_key_but_right_sha_returns_422():
    recs = b"".join(
        raw_record(f"<urn:uuid:{i}>", uri=f"http://e{i}.example/",
                   block=http_block(body=b"x"))
        for i in range(3))
    # SHA 正确（同包），但 URL/时刻与归档中该序号的记录不符
    cursor = encode_cursor({
        "v": 1,
        "sha": hashlib.sha256(recs).hexdigest(),
        "url": "http://e1.example/",
        "ts": "2020-01-01T00:00:00Z",
        "order": 0,  # 序号 0 实际对应 http://e0.example/
    })
    r = post_index(recs, page_size=1, cursor=cursor)
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_cursor"


# ---------- page_size / 请求模型校验 ----------


def test_page_size_boundaries():
    enc = base64.b64encode(
        raw_record("<urn:uuid:a>", block=http_block(body=b"x"))).decode()
    for bad in (0, 101, -1, "abc", 3.5, [3], None):
        payload = {"warc": enc, "page_size": bad}
        assert client.post("/v1/capture-index", json=payload).status_code == 422, bad
    # 缺 page_size / 缺 warc 同样 422
    assert client.post("/v1/capture-index",
                       json={"warc": enc}).status_code == 422
    assert client.post("/v1/capture-index",
                       json={"page_size": 10}).status_code == 422
    # 边界值合法
    assert client.post("/v1/capture-index",
                       json={"warc": enc, "page_size": 1}).status_code == 200
    assert client.post("/v1/capture-index",
                       json={"warc": enc, "page_size": 100}).status_code == 200


def test_pagination_across_same_instant_duplicates():
    # 同一 URL、同一 UTC 时刻的多条记录：cursor 必须靠归档序号区分，
    # 翻页不丢条、不重条
    recs = b"".join(
        raw_record(f"<urn:uuid:{i}>", date="2020-01-01T01:00:00+01:00",
                   block=http_block(body=str(i).encode()))
        for i in range(5))
    seen, cursor = [], None
    for _ in range(5):
        r = post_index(recs, page_size=2, cursor=cursor)
        assert r.status_code == 200, r.text
        data = r.json()
        seen.extend(e["record_id"] for e in data["entries"])
        cursor = data["next_cursor"]
        if cursor is None:
            break
    assert seen == [f"<urn:uuid:{i}>" for i in range(5)]


def test_cursor_roundtrip_with_non_ascii_url():
    # cursor 中的非 ASCII URL 经 UTF-8 + Base64URL 往返
    recs = b"".join(
        raw_record(f"<urn:uuid:{i}>", uri=f"http://例え.example/{i}",
                   block=http_block(body=b"x"))
        for i in range(3))
    r1 = post_index(recs, page_size=1)
    assert r1.status_code == 200, r1.text
    cursor = r1.json()["next_cursor"]
    # 纯 Base64URL 字符（无 +/= 空白）
    assert all(ch.isascii() and ch not in "+/=" for ch in cursor)
    r2 = post_index(recs, page_size=1, cursor=cursor)
    assert r2.status_code == 200, r2.text
    assert [e["record_id"] for e in r2.json()["entries"]] == ["<urn:uuid:1>"]
    assert r2.json()["entries"][0]  # 同包继续翻页有效


def test_cursor_must_be_string():
    enc = base64.b64encode(
        raw_record("<urn:uuid:a>", block=http_block(body=b"x"))).decode()
    r = client.post("/v1/capture-index",
                    json={"warc": enc, "page_size": 1, "cursor": 123})
    assert r.status_code == 422


# ---------- 其余校验沿用现有规则 ----------


def test_warc_errors_passthrough():
    r = client.post("/v1/capture-index",
                    json={"warc": "!!!not-base64!!!", "page_size": 10})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_base64"

    blob = b"x" * (MAX_DECODED_BYTES + 1)
    r = client.post("/v1/capture-index", json={
        "warc": base64.b64encode(blob).decode(), "page_size": 10})
    assert r.status_code == 422
    assert r.json()["error"] == "payload_too_large"

    assert post_index(b"this is not a warc file at all").status_code == 422


def test_whole_archive_validated_even_unlisted_records():
    # 未被列出（缺 URL）的 revisit 引用链断裂，仍使整包 422
    good = raw_record("<urn:uuid:good>", block=http_block(body=b"g"))
    bad = raw_record(
        "<urn:uuid:bad>", rec_type="revisit", block=b"", with_uri=False,
        extra_headers=b"WARC-Refers-To: <urn:uuid:ghost>\r\n")
    r = post_index(good + bad)
    assert r.status_code == 422
    assert r.json()["error"] == "target_not_found"

    # 重复 ID 与 /v1/replay 一样判错
    dup = raw_record("<urn:uuid:good>", block=http_block(body=b"dup"))
    r = post_index(good + dup)
    assert r.status_code == 422
    assert r.json()["error"] == "duplicate_record_id"

    # response 缺有效 HTTP 头：整包 422（即使它缺 URL）
    no_http = raw_record("<urn:uuid:noh>", block=b"", with_uri=False)
    r = post_index(no_http)
    assert r.status_code == 422
    assert r.json()["error"] == "response_without_http_headers"
