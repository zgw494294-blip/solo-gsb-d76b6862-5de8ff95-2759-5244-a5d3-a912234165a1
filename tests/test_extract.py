"""POST /v1/extract 端到端测试：子集导出、依赖闭包、字节保留与回放等价。"""

from __future__ import annotations

import base64
import gzip
import io

from fastapi.testclient import TestClient
from warcio.archiveiterator import ArchiveIterator
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import BufferWARCWriter

from app.main import app
from app.replay import MAX_DECODED_BYTES

client = TestClient(app)


# ---------- WARC 构造辅助（与 test_replay.py 同风格） ----------


def _writer_record(w, rec_id, rec_type="response", uri="http://example.com/",
                   date="2020-01-01T00:00:00Z", body=b"", status="200 OK",
                   extra_headers=None, with_http=True):
    http = (
        StatusAndHeaders(status, [("Content-Length", str(len(body)))],
                         protocol="HTTP/1.1")
        if with_http
        else None
    )
    hdrs = {"WARC-Date": date, "WARC-Record-ID": rec_id, **(extra_headers or {})}
    return w.create_warc_record(
        uri, rec_type, http_headers=http,
        payload=io.BytesIO(body), warc_headers_dict=hdrs,
    )


def build_warc(records_writer_fn, gzip_=False):
    w = BufferWARCWriter(gzip=gzip_)
    records_writer_fn(w)
    return w.get_contents()


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


def post_extract(warc_bytes, record_ids, **kw):
    return client.post(
        "/v1/extract",
        json={"warc": base64.b64encode(warc_bytes).decode(),
              "record_ids": record_ids},
        **kw,
    )


def export_bytes(response):
    assert response.status_code == 200, response.text
    return base64.b64decode(response.json()["warc"])


def replay_records(warc_bytes):
    r = client.post("/v1/replay",
                    json={"warc": base64.b64encode(warc_bytes).decode()})
    assert r.status_code == 200, r.text
    return r.json()["records"]


# ---------- 正常路径 ----------


def test_extract_single_response():
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"hello"))
    b = raw_record("<urn:uuid:b>", block=http_block(body=b"world"),
                   uri="http://b.example/")
    r = post_extract(a + b, ["<urn:uuid:a>"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["included_ids"] == ["<urn:uuid:a>"]
    records = replay_records(export_bytes(r))
    assert len(records) == 1
    assert records[0]["record_id"] == "<urn:uuid:a>"
    assert records[0]["http_status"] == 200
    assert base64.b64decode(records[0]["body_base64"]) == b"hello"
    assert records[0]["body_source_id"] == "<urn:uuid:a>"


def test_extract_revisit_pulls_forward_endpoint():
    # 终点 response 位于所请 revisit 之后（前向引用），也必须纳入
    rev = raw_record(
        "<urn:uuid:rev>", rec_type="revisit", block=b"",
        extra_headers=b"WARC-Refers-To: <urn:uuid:later>\r\n")
    resp = raw_record("<urn:uuid:later>", rec_type="response",
                      block=http_block(body=b"later-body"))
    other = raw_record("<urn:uuid:other>", block=http_block(body=b"other"),
                       uri="http://other.example/")
    r = post_extract(rev + resp + other, ["<urn:uuid:rev>"])
    assert r.status_code == 200, r.text
    # included_ids 按归档原序（rev 在前、later 在后），other 被丢弃
    assert r.json()["included_ids"] == ["<urn:uuid:rev>", "<urn:uuid:later>"]
    records = replay_records(export_bytes(r))
    assert [x["record_id"] for x in records] == [
        "<urn:uuid:rev>", "<urn:uuid:later>"]
    assert base64.b64decode(records[0]["body_base64"]) == b"later-body"
    assert records[0]["body_source_id"] == "<urn:uuid:later>"


def test_extract_multilevel_chain_includes_intermediate_revisits():
    # v1 -> v2 -> a；请求 v1 须纳入中间 revisit v2 与终点 response a
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"ORIGINAL"))
    v1 = raw_record(
        "<urn:uuid:v1>", rec_type="revisit", block=b"",
        uri="http://v1.example/", date="2021-01-01T00:00:00Z",
        extra_headers=b"WARC-Refers-To: <urn:uuid:v2>\r\n")
    v2 = raw_record(
        "<urn:uuid:v2>", rec_type="revisit", block=b"",
        uri="http://v2.example/", date="2021-02-02T00:00:00Z",
        extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    r = post_extract(a + v1 + v2, ["<urn:uuid:v1>"])
    assert r.status_code == 200, r.text
    assert r.json()["included_ids"] == [
        "<urn:uuid:a>", "<urn:uuid:v1>", "<urn:uuid:v2>"]
    records = replay_records(export_bytes(r))
    for rec in records:
        assert base64.b64decode(rec["body_base64"]) == b"ORIGINAL"
        assert rec["body_source_id"] == "<urn:uuid:a>"


def test_extract_drops_unrelated_and_non_target_records():
    def fill(w):
        w.write_record(w.create_warc_record(
            None, "warcinfo", payload=io.BytesIO(b"info: x\r\n"),
            warc_headers_dict={"WARC-Date": "2020-01-01T00:00:00Z",
                               "WARC-Record-ID": "<urn:uuid:info>"}))
        w.write_record(_writer_record(w, "<urn:uuid:a>", body=b"aa"))
        w.write_record(w.create_warc_record(
            "http://example.com/", "request",
            http_headers=StatusAndHeaders("GET / HTTP/1.1", [],
                                          protocol="HTTP/1.1"),
            warc_headers_dict={"WARC-Date": "2020-01-01T00:00:00Z",
                               "WARC-Record-ID": "<urn:uuid:req>"}))
        w.write_record(_writer_record(w, "<urn:uuid:b>", body=b"bb",
                                      uri="http://b.example/"))
    r = post_extract(build_warc(fill), ["<urn:uuid:a>"])
    assert r.status_code == 200, r.text
    assert r.json()["included_ids"] == ["<urn:uuid:a>"]
    records = replay_records(export_bytes(r))
    assert [x["record_id"] for x in records] == ["<urn:uuid:a>"]


def test_extract_shared_dependency_included_once():
    # 两条 revisit 指向同一终点 response：终点只纳入一次
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"shared"))
    v1 = raw_record(
        "<urn:uuid:v1>", rec_type="revisit", block=b"",
        uri="http://v1.example/", date="2021-01-01T00:00:00Z",
        extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    v2 = raw_record(
        "<urn:uuid:v2>", rec_type="revisit", block=b"",
        uri="http://v2.example/", date="2021-02-02T00:00:00Z",
        extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    r = post_extract(a + v1 + v2, ["<urn:uuid:v1>", "<urn:uuid:v2>"])
    assert r.status_code == 200, r.text
    assert r.json()["included_ids"] == [
        "<urn:uuid:a>", "<urn:uuid:v1>", "<urn:uuid:v2>"]


def test_extract_archive_order_not_request_order():
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))
    b = raw_record("<urn:uuid:b>", block=http_block(body=b"bb"),
                   uri="http://b.example/")
    r = post_extract(a + b, ["<urn:uuid:b>", "<urn:uuid:a>"])
    assert r.status_code == 200, r.text
    assert r.json()["included_ids"] == ["<urn:uuid:a>", "<urn:uuid:b>"]
    records = replay_records(export_bytes(r))
    assert [x["record_id"] for x in records] == [
        "<urn:uuid:a>", "<urn:uuid:b>"]


def test_extract_id_normalization_and_dedup():
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))
    b = raw_record("urn:uuid:b", block=http_block(body=b"bb"),
                   uri="http://b.example/")
    # 裸 URN 与尖括号形式互通、空白容忍、重复去重
    r = post_extract(a + b, ["urn:uuid:a", "<urn:uuid:a>", "  urn:uuid:a  ",
                             "<urn:uuid:b>"])
    assert r.status_code == 200, r.text
    assert r.json()["included_ids"] == ["<urn:uuid:a>", "urn:uuid:b"]


def test_extract_gzip_input():
    def fill(w):
        w.write_record(_writer_record(w, "<urn:uuid:a>", body=b"gz"))
    r = post_extract(build_warc(fill, gzip_=True), ["<urn:uuid:a>"])
    assert r.status_code == 200, r.text
    records = replay_records(export_bytes(r))
    assert base64.b64decode(records[0]["body_base64"]) == b"gz"

    # 整条单流 gzip 也可
    r = post_extract(gzip.compress(build_warc(fill)), ["<urn:uuid:a>"])
    assert r.status_code == 200, r.text


def test_extract_max_record_ids_accepted():
    # 恰好 100 个 ID 合法
    recs = b"".join(
        raw_record(f"<urn:uuid:{i}>", block=http_block(body=b"x"),
                   uri=f"http://e{i}.example/")
        for i in range(100)
    )
    ids = [f"<urn:uuid:{i}>" for i in range(100)]
    r = post_extract(recs, ids)
    assert r.status_code == 200, r.text
    assert r.json()["included_ids"] == ids


# ---------- 字节保留与回放等价 ----------


def test_extract_preserves_record_bytes_verbatim():
    # 单记录导出：导出字节 == 原记录字节（去掉记录尾部分隔符）
    block = http_block("HTTP/1.1 200 OK",
                       [("Content-Type", "text/plain; charset=utf-8"),
                        ("X-Custom", "v1")], body=b"\x00\x01payload\xff")
    rec = raw_record(
        "<urn:uuid:a>", block=block, date="2021-05-05T10:00:00Z",
        extra_headers=b"Content-Type: application/http; msgtype=response\r\n"
                      b"WARC-Block-Digest: sha1:ABC\r\n")
    assert rec.endswith(b"\r\n\r\n")
    r = post_extract(rec, ["<urn:uuid:a>"])
    assert r.status_code == 200, r.text
    assert export_bytes(r) == rec[: -len(b"\r\n\r\n")]


def test_extract_export_content_length_matches_written_bytes():
    # 逐条核对导出包中每条记录声明的 Content-Length 与实际块字节数
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"hello"))
    rev = raw_record(
        "<urn:uuid:rev>", rec_type="revisit",
        block=http_block("HTTP/1.1 304 Not Modified", body=b""),
        extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    r = post_extract(a + rev, ["<urn:uuid:rev>"])
    data = export_bytes(r)
    seen = []
    for rec in ArchiveIterator(io.BytesIO(data)):
        declared = int(rec.rec_headers.get_header("Content-Length"))
        rec.raw_stream.read()
        # tell() 含 warcio 解析时预读的 HTTP 头，等于整块消费量
        assert rec.raw_stream.tell() == declared
        seen.append(rec.rec_headers.get_header("WARC-Record-ID"))
    assert seen == ["<urn:uuid:a>", "<urn:uuid:rev>"]


def test_extract_export_replays_identically_to_original():
    # 混合归档：多级链、revisit 自带 304 头、前向引用、无关记录
    v1 = raw_record(
        "<urn:uuid:v1>", rec_type="revisit", block=b"",
        uri="http://v1.example/", date="2021-01-01T00:00:00Z",
        extra_headers=b"WARC-Refers-To: <urn:uuid:v2>\r\n")
    x = raw_record("<urn:uuid:x>", block=http_block(
        "HTTP/1.1 404 Not Found", body=b"OTHER"), uri="http://x.example/")
    v2 = raw_record(
        "<urn:uuid:v2>", rec_type="revisit", block=b"",
        uri="http://v2.example/", date="2021-02-02T00:00:00Z",
        extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    r304 = raw_record(
        "<urn:uuid:r304>", rec_type="revisit",
        block=http_block("HTTP/1.1 304 Not Modified", body=b""),
        uri="http://r304.example/", date="2022-03-03T00:00:00Z",
        extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"ORIGINAL"),
                   uri="http://a.example/")
    archive = v1 + x + v2 + r304 + a

    requested = ["<urn:uuid:v1>", "<urn:uuid:r304>", "<urn:uuid:a>"]
    r = post_extract(archive, requested)
    assert r.status_code == 200, r.text
    # 归档原序：v1, v2（依赖）, r304, a；x 被丢弃
    assert r.json()["included_ids"] == [
        "<urn:uuid:v1>", "<urn:uuid:v2>", "<urn:uuid:r304>", "<urn:uuid:a>"]

    original = {rec["record_id"]: rec for rec in replay_records(archive)}
    exported = {rec["record_id"]: rec for rec in replay_records(export_bytes(r))}
    assert set(exported) == set(r.json()["included_ids"])
    for rid in requested:
        o, e = original[rid], exported[rid]
        for field in ("record_type", "target_uri", "capture_time",
                      "http_status", "body_base64", "body_source_id"):
            assert e[field] == o[field], (rid, field)
    # 依赖记录同样逐字段一致
    assert exported["<urn:uuid:v2>"]["body_base64"] == \
        original["<urn:uuid:v2>"]["body_base64"]
    assert exported["<urn:uuid:r304>"]["http_status"] == 304


def test_extract_record_without_content_length_roundtrip():
    # 缺 Content-Length 的记录块延伸至 EOF：导出后还原正文必须逐字节一致
    rec = (b"WARC/1.1\r\nWARC-Type: response\r\nWARC-Record-ID: <urn:uuid:a>\r\n"
           b"WARC-Date: 2020-01-01T00:00:00Z\r\n"
           b"WARC-Target-URI: http://example.com/\r\n"
           b"\r\nHTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello\r\n\r\n")
    original = replay_records(rec)
    r = post_extract(rec, ["<urn:uuid:a>"])
    assert r.status_code == 200, r.text
    exported = replay_records(export_bytes(r))
    assert exported[0]["body_base64"] == original[0]["body_base64"]


def test_extract_leading_blank_lines_rejected_like_replay():
    # 归档开头的前导空行与 /v1/replay 一样判 malformed_warc（沿用同一套校验）
    rec = raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))
    r = post_extract(b"\r\n\r\n" + rec, ["<urn:uuid:a>"])
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"


# ---------- 422 错误场景 ----------


def test_extract_unknown_record_id():
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))
    r = post_extract(a, ["<urn:uuid:ghost>"])
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "unknown_record_id"
    assert d["record_id"] == "<urn:uuid:ghost>"


def test_extract_unknown_id_among_valid_ones():
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))
    r = post_extract(a, ["<urn:uuid:a>", "<urn:uuid:ghost>"])
    assert r.status_code == 422
    assert r.json()["error"] == "unknown_record_id"


def test_extract_unknown_id_for_non_target_record_type():
    # warcinfo/request 等类型不建索引，其 ID 视为未知
    def fill(w):
        w.write_record(w.create_warc_record(
            None, "warcinfo", payload=io.BytesIO(b"info: x\r\n"),
            warc_headers_dict={"WARC-Date": "2020-01-01T00:00:00Z",
                               "WARC-Record-ID": "<urn:uuid:info>"}))
        w.write_record(_writer_record(w, "<urn:uuid:a>", body=b"aa"))
    r = post_extract(build_warc(fill), ["<urn:uuid:info>"])
    assert r.status_code == 422
    assert r.json()["error"] == "unknown_record_id"


def test_extract_empty_archive_unknown_id():
    r = post_extract(b"", ["<urn:uuid:a>"])
    assert r.status_code == 422
    assert r.json()["error"] == "unknown_record_id"


def test_extract_record_ids_count_constraints():
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))
    enc = base64.b64encode(a).decode()
    assert client.post("/v1/extract",
                       json={"warc": enc, "record_ids": []}).status_code == 422
    assert client.post(
        "/v1/extract",
        json={"warc": enc, "record_ids": ["<urn:uuid:a>"] * 101},
    ).status_code == 422


def test_extract_record_ids_type_constraints():
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))
    enc = base64.b64encode(a).decode()
    # 非字符串元素 / 非列表 / 缺字段 / 缺 warc 一律 422
    assert client.post("/v1/extract", json={
        "warc": enc, "record_ids": [123]}).status_code == 422
    assert client.post("/v1/extract", json={
        "warc": enc, "record_ids": "<urn:uuid:a>"}).status_code == 422
    assert client.post("/v1/extract", json={"warc": enc}).status_code == 422
    assert client.post("/v1/extract", json={
        "record_ids": ["<urn:uuid:a>"]}).status_code == 422


def test_extract_warc_errors_passthrough():
    # 与 /v1/replay 相同的输入/结构错误
    r = client.post("/v1/extract", json={"warc": "!!!not-base64!!!",
                                         "record_ids": ["x"]})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_base64"

    blob = b"x" * (MAX_DECODED_BYTES + 1)
    r = client.post("/v1/extract", json={
        "warc": base64.b64encode(blob).decode(), "record_ids": ["x"]})
    assert r.status_code == 422
    assert r.json()["error"] == "payload_too_large"

    r = post_extract(b"this is not a warc file at all", ["<urn:uuid:a>"])
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"


def test_extract_validates_whole_archive_even_unrequested():
    # 未被请求的坏记录（断链 revisit）同样使整个请求 422
    good = raw_record("<urn:uuid:good>", block=http_block(body=b"good"))
    bad = raw_record(
        "<urn:uuid:bad>", rec_type="revisit", block=b"",
        extra_headers=b"WARC-Refers-To: <urn:uuid:ghost>\r\n")
    r = post_extract(good + bad, ["<urn:uuid:good>"])
    assert r.status_code == 422
    assert r.json()["error"] == "target_not_found"

    # 重复 ID 也一样
    dup = raw_record("<urn:uuid:good>", block=http_block(body=b"dup"))
    r = post_extract(good + dup, ["<urn:uuid:good>"])
    assert r.status_code == 422
    assert r.json()["error"] == "duplicate_record_id"
