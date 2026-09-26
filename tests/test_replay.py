"""端到端测试，覆盖需求中的全部正常路径与 422 错误场景。"""

from __future__ import annotations

import base64
import gzip
import io
import zlib

import pytest
from fastapi.testclient import TestClient
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import BufferWARCWriter

from app.main import app
from app.replay import MAX_DECODED_BYTES

client = TestClient(app)


# ---------- WARC 构造辅助 ----------


def _writer_record(w, rec_id, rec_type="response", uri="http://example.com/",
                   date="2020-01-01T00:00:00Z", body=b"", status="200 OK",
                   extra_headers=None, with_http=True):
    http = (
        StatusAndHeaders(status, [("Content-Length", str(len(body)))], protocol="HTTP/1.1")
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


def post(warc_bytes, **kw):
    return client.post(
        "/v1/replay", json={"warc": base64.b64encode(warc_bytes).decode()}, **kw
    )


# ---------- 健康检查 ----------


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ---------- 正常路径 ----------


def test_basic_response():
    def fill(w):
        w.write_record(_writer_record(w, "<urn:uuid:a>", body=b"hello",
                                      uri="http://a.example/", date="2021-05-05T10:00:00Z"))
    r = post(build_warc(fill))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 1
    rec = data["records"][0]
    assert rec["record_type"] == "response"
    assert rec["target_uri"] == "http://a.example/"
    assert rec["capture_time"] == "2021-05-05T10:00:00Z"
    assert rec["http_status"] == 200
    assert base64.b64decode(rec["body_base64"]) == b"hello"
    assert rec["body_source_id"] == "<urn:uuid:a>"


def test_empty_body_is_legal():
    def fill(w):
        w.write_record(_writer_record(w, "<urn:uuid:a>", body=b""))
    r = post(build_warc(fill))
    assert r.status_code == 200
    rec = r.json()["records"][0]
    assert rec["body_base64"] == ""
    assert rec["http_status"] == 200


def test_revisit_backward_and_multilevel_reference():
    # a(response) <- v2(revisit) <- v1(revisit)，归档顺序为 a, v1, v2
    def fill(w):
        w.write_record(_writer_record(w, "<urn:uuid:a>", body=b"ORIGINAL", status="200 OK"))
        w.write_record(_writer_record(
            w, "<urn:uuid:v1>", rec_type="revisit", with_http=False,
            uri="http://v1.example/", date="2021-01-01T00:00:00Z",
            extra_headers={"WARC-Refers-To": "<urn:uuid:v2>"}))
        w.write_record(_writer_record(
            w, "<urn:uuid:v2>", rec_type="revisit", with_http=False,
            uri="http://v2.example/", date="2021-02-02T00:00:00Z",
            extra_headers={"WARC-Refers-To": "<urn:uuid:a>"}))
    r = post(build_warc(fill))
    assert r.status_code == 200, r.text
    records = r.json()["records"]
    assert [x["record_id"] for x in records] == ["<urn:uuid:a>", "<urn:uuid:v1>", "<urn:uuid:v2>"]
    assert [x["target_uri"] for x in records] == [
        "http://example.com/", "http://v1.example/", "http://v2.example/"]
    assert [x["http_status"] for x in records] == [200, None, None]
    for rec in records:
        assert base64.b64decode(rec["body_base64"]) == b"ORIGINAL"
        assert rec["body_source_id"] == "<urn:uuid:a>"


def test_forward_reference_allowed():
    # revisit 出现在被引用的 response 之前
    rev = raw_record(
        "<urn:uuid:rev>", rec_type="revisit", block=b"",
        extra_headers=b"WARC-Refers-To: <urn:uuid:later>\r\n")
    resp = raw_record(
        "<urn:uuid:later>", rec_type="response",
        block=http_block(body=b"later-body"))
    r = post(rev + resp)
    assert r.status_code == 200, r.text
    records = r.json()["records"]
    assert records[0]["record_id"] == "<urn:uuid:rev>"
    assert base64.b64decode(records[0]["body_base64"]) == b"later-body"
    assert records[0]["body_source_id"] == "<urn:uuid:later>"
    assert records[0]["http_status"] is None
    assert records[1]["http_status"] == 200


def test_revisit_keeps_own_url_time_status_inherits_body():
    block = http_block("HTTP/1.1 304 Not Modified", body=b"")
    rev = raw_record(
        "<urn:uuid:rev>", rec_type="revisit", block=block,
        uri="http://rev.example/", date="2022-03-03T00:00:00Z",
        extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    resp = raw_record("<urn:uuid:a>", rec_type="response",
                      block=http_block("HTTP/1.1 200 OK", body=b"inherited"))
    r = post(rev + resp)
    assert r.status_code == 200
    rec = r.json()["records"][0]
    assert rec["target_uri"] == "http://rev.example/"
    assert rec["capture_time"] == "2022-03-03T00:00:00Z"
    assert rec["http_status"] == 304
    assert base64.b64decode(rec["body_base64"]) == b"inherited"
    assert rec["body_source_id"] == "<urn:uuid:a>"


def test_archive_order_preserved_with_non_target_records():
    # warcinfo / request / metadata 被忽略，但 response/revisit 保持原序
    def fill(w):
        w.write_record(w.create_warc_record(
            None, "warcinfo", payload=io.BytesIO(b"info: x\r\n"),
            warc_headers_dict={"WARC-Date": "2020-01-01T00:00:00Z",
                               "WARC-Record-ID": "<urn:uuid:info>"}))
        w.write_record(_writer_record(w, "<urn:uuid:a>", body=b"aa"))
        w.write_record(w.create_warc_record(
            "http://example.com/", "request",
            http_headers=StatusAndHeaders("GET / HTTP/1.1", [], protocol="HTTP/1.1"),
            warc_headers_dict={"WARC-Date": "2020-01-01T00:00:00Z",
                               "WARC-Record-ID": "<urn:uuid:req>"}))
        w.write_record(_writer_record(w, "<urn:uuid:b>", body=b"",
                                      rec_type="revisit", with_http=False,
                                      extra_headers={"WARC-Refers-To": "<urn:uuid:a>"}))
    r = post(build_warc(fill))
    assert r.status_code == 200
    assert [x["record_id"] for x in r.json()["records"]] == [
        "<urn:uuid:a>", "<urn:uuid:b>"]


def test_gzip_single_stream_and_multimember():
    def fill(w):
        w.write_record(_writer_record(w, "<urn:uuid:a>", body=b"gz"))
    per_record_gz = build_warc(fill, gzip_=True)
    assert per_record_gz[:2] == b"\x1f\x8b"
    assert post(per_record_gz).status_code == 200

    # 整条单流 gzip（非 per-record member）也应可用
    single_gz = gzip.compress(build_warc(fill))
    r = post(single_gz)
    assert r.status_code == 200
    assert base64.b64decode(r.json()["records"][0]["body_base64"]) == b"gz"


def test_base64_with_whitespace_and_urlsafe():
    def fill(w):
        w.write_record(_writer_record(w, "<urn:uuid:a>", body=b"x"))
    raw = build_warc(fill)
    enc = base64.b64encode(raw).decode()
    spaced = "\n".join(enc[i:i + 16] for i in range(0, len(enc), 16))
    r = client.post("/v1/replay", json={"warc": spaced})
    assert r.status_code == 200

    urlsafe = base64.urlsafe_b64encode(raw).decode()
    r = client.post("/v1/replay", json={"warc": urlsafe})
    assert r.status_code == 200


def test_empty_warc_file_returns_empty_list():
    # 空 WARC 文件（0 字节）合法，没有任何记录
    r = post(b"")
    assert r.status_code == 200
    assert r.json() == {"count": 0, "records": []}


def test_http_statuses_and_headers_variety():
    for status_line, code in (("HTTP/1.1 404 Not Found", 404),
                              ("HTTP/1.0 301 Moved", 301),
                              ("HTTP/1.1 204 No Content", 204)):
        rec = raw_record(f"<urn:uuid:{code}>", block=http_block(status_line, body=b""))
        r = post(rec)
        assert r.status_code == 200, status_line
        assert r.json()["records"][0]["http_status"] == code


# ---------- 422 错误场景 ----------


def test_missing_record_id():
    rec = (b"WARC/1.1\r\nWARC-Type: response\r\nWARC-Date: d\r\n"
           b"WARC-Target-URI: http://e.com/\r\nContent-Length: 0\r\n\r\n\r\n\r\n")
    r = post(rec)
    assert r.status_code == 422
    assert r.json()["error"] == "missing_record_id"


def test_response_without_target_uri_still_rejected_by_replay():
    # /v1/replay 保持原有严格校验：带有效 HTTP 头但缺 WARC-Target-URI 的
    # response 仍判 malformed_warc（对该字段的放宽只适用于 /v1/navigate）。
    rec = raw_record("<urn:uuid:nouri>", block=http_block(body=b"orphan"))
    head, sep, tail = rec.partition(b"WARC-Target-URI: http://example.com/\r\n")
    rec_no_uri = head + tail
    assert b"WARC-Target-URI" not in rec_no_uri
    r = post(rec_no_uri)
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"

    # 同包存在另一条完好记录时也不能只返回前者
    good = raw_record("<urn:uuid:good>", block=http_block(body=b"good"))
    r = post(rec_no_uri + good)
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"


def test_duplicate_record_id():
    def fill(w):
        w.write_record(_writer_record(w, "<urn:uuid:dup>", body=b"1"))
        w.write_record(_writer_record(w, "<urn:uuid:dup>", body=b"2"))
    r = post(build_warc(fill))
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "duplicate_record_id"
    assert d["record_id"] == "<urn:uuid:dup>"


def test_revisit_target_missing():
    rev = raw_record(
        "<urn:uuid:rev>", rec_type="revisit", block=b"",
        extra_headers=b"WARC-Refers-To: <urn:uuid:ghost>\r\n")
    r = post(rev)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "target_not_found"
    assert d["record_id"] == "<urn:uuid:rev>"
    assert d["missing_id"] == "<urn:uuid:ghost>"
    assert d["chain"] == ["<urn:uuid:rev>", "<urn:uuid:ghost>"]


def test_revisit_self_cycle():
    rev = raw_record(
        "<urn:uuid:rev>", rec_type="revisit", block=b"",
        extra_headers=b"WARC-Refers-To: <urn:uuid:rev>\r\n")
    r = post(rev)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "reference_cycle"
    assert "<urn:uuid:rev>" in d["chain"]


def test_revisit_cycle_chain_reported():
    # v1 -> v2 -> v3 -> v2
    def rev(rid, target):
        return raw_record(
            rid, rec_type="revisit", block=b"",
            extra_headers=f"WARC-Refers-To: {target}\r\n".encode())
    data = rev("<urn:uuid:v1>", "<urn:uuid:v2>") + rev("<urn:uuid:v2>", "<urn:uuid:v3>") \
        + rev("<urn:uuid:v3>", "<urn:uuid:v2>")
    r = post(data)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "reference_cycle"
    assert d["record_id"] == "<urn:uuid:v1>"
    assert d["chain"] == ["<urn:uuid:v1>", "<urn:uuid:v2>", "<urn:uuid:v3>", "<urn:uuid:v2>"]


def test_revisit_without_refers_to():
    rev = raw_record("<urn:uuid:rev>", rec_type="revisit", block=b"")
    r = post(rev)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "chain_broken"
    assert d["broken_record_id"] == "<urn:uuid:rev>"
    assert d["chain"] == ["<urn:uuid:rev>"]


def test_endpoint_without_http_headers():
    rev = raw_record(
        "<urn:uuid:rev>", rec_type="revisit", block=b"",
        extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    resp = raw_record("<urn:uuid:a>", rec_type="response", block=b"")
    r = post(rev + resp)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "endpoint_without_http_headers"
    assert d["endpoint_record_id"] == "<urn:uuid:a>"
    assert d["chain"] == ["<urn:uuid:rev>", "<urn:uuid:a>"]


def test_direct_response_without_http_headers():
    resp = raw_record("<urn:uuid:a>", rec_type="response", block=b"")
    r = post(resp)
    assert r.status_code == 422
    assert r.json()["error"] == "response_without_http_headers"


def test_endpoint_with_garbage_http_line():
    # "HTTP 响应头"位置实际是普通文本（无状态行）
    rev = raw_record(
        "<urn:uuid:rev>", rec_type="revisit", block=b"",
        extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    resp = raw_record("<urn:uuid:a>", rec_type="response",
                      block=b"not an http response at all")
    r = post(rev + resp)
    assert r.status_code == 422
    assert r.json()["error"] == "endpoint_without_http_headers"


def test_malformed_warc_garbage():
    r = post(b"this is not a warc file at all")
    assert r.status_code == 422
    assert r.json()["error"] in ("malformed_warc",)


def test_malformed_warc_truncated_headers():
    r = post(b"WARC/1.1\r\nWARC-Type: response\r\n")
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"


def test_truncated_block_content_length_exceeds_actual():
    # WARC Content-Length 大于实际块内容：HTTP 头完整，但记录块整体短读
    block = http_block(body=b"hello")
    rec = (
        b"WARC/1.1\r\nWARC-Type: response\r\nWARC-Record-ID: <urn:uuid:trunc>\r\n"
        b"WARC-Date: 2020-01-01T00:00:00Z\r\nWARC-Target-URI: http://example.com/\r\n"
        b"Content-Length: " + str(len(block) + 100).encode() + b"\r\n\r\n"
        + block + b"\r\n\r\n"
    )
    r = post(rec)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "malformed_warc"
    assert d["record_order"] == 0
    assert d["record_id"] == "<urn:uuid:trunc>"
    assert d["declared_length"] == len(block) + 100
    assert d["actual_length"] < d["declared_length"]


def test_truncated_block_at_eof_without_separator():
    # 文件在记录块中间直接结束（连记录分隔符都没有）
    block = http_block(body=b"hello")
    rec = (
        b"WARC/1.1\r\nWARC-Type: response\r\nWARC-Record-ID: <urn:uuid:trunc>\r\n"
        b"WARC-Date: 2020-01-01T00:00:00Z\r\nWARC-Target-URI: http://example.com/\r\n"
        b"Content-Length: " + str(len(block) + 100).encode() + b"\r\n\r\n" + block
    )
    r = post(rec)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "malformed_warc"
    assert d["record_id"] == "<urn:uuid:trunc>"


def test_truncated_second_record_after_valid_one():
    # 第一条完好，第二条截断：报错并给出正确的记录序号
    good = raw_record("<urn:uuid:ok>", block=http_block(body=b"fine"))
    block = http_block(body=b"partial")
    bad = (
        b"WARC/1.1\r\nWARC-Type: response\r\nWARC-Record-ID: <urn:uuid:bad>\r\n"
        b"WARC-Date: 2020-01-01T00:00:00Z\r\nWARC-Target-URI: http://example.com/\r\n"
        b"Content-Length: " + str(len(block) + 50).encode() + b"\r\n\r\n" + block
    )
    r = post(good + bad)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "malformed_warc"
    assert d["record_order"] == 1
    assert d["record_id"] == "<urn:uuid:bad>"


def test_truncated_skipped_record_type_still_checked():
    # warcinfo 不参与还原，但块截断同样要判 malformed_warc
    info = (
        b"WARC/1.1\r\nWARC-Type: warcinfo\r\nWARC-Record-ID: <urn:uuid:info>\r\n"
        b"WARC-Date: 2020-01-01T00:00:00Z\r\nContent-Length: 500\r\n\r\n"
        b"short\r\n\r\n"
    )
    r = post(info)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "malformed_warc"
    assert d["record_order"] == 0
    assert d["record_id"] == "<urn:uuid:info>"


def test_truncated_record_without_id_reports_order_only():
    # 无法取得 ID 时仍报 malformed_warc，附记录序号、不带 record_id
    rec = (
        b"WARC/1.1\r\nWARC-Type: response\r\nWARC-Date: 2020-01-01T00:00:00Z\r\n"
        b"WARC-Target-URI: http://e.com/\r\nContent-Length: 50\r\n\r\nabc"
    )
    r = post(rec)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "malformed_warc"
    assert d["record_order"] == 0
    assert "record_id" not in d


def test_truncated_block_inside_gzip():
    # gzip 本身完好，但解压出的 WARC 记录块截断（单流与多 member 两种形态）
    block = http_block(body=b"hello")
    rec = (
        b"WARC/1.1\r\nWARC-Type: response\r\nWARC-Record-ID: <urn:uuid:trunc>\r\n"
        b"WARC-Date: 2020-01-01T00:00:00Z\r\nWARC-Target-URI: http://example.com/\r\n"
        b"Content-Length: " + str(len(block) + 100).encode() + b"\r\n\r\n" + block
    )
    r = post(gzip.compress(rec))
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"

    half = len(rec) // 2
    multi_member = gzip.compress(rec[:half]) + gzip.compress(rec[half:])
    r = post(multi_member)
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"


def _zero_block_truncated_record(rec_id=b"<urn:uuid:trunc>", rec_type=b"response",
                                  declared=100, with_id=True):
    """WARC 头完整、Content-Length 为正，头后直接 EOF（块 0 字节）。"""
    head = (
        b"WARC/1.1\r\nWARC-Type: " + rec_type + b"\r\n"
        + (b"WARC-Record-ID: " + rec_id + b"\r\n" if with_id else b"")
        + b"WARC-Date: 2020-01-01T00:00:00Z\r\n"
        b"WARC-Target-URI: http://example.com/\r\n"
        b"Content-Length: " + str(declared).encode() + b"\r\n\r\n"
    )
    return head


def test_zero_byte_final_record_at_eof_detected():
    # 头完整、Content-Length 为正、头后直接 EOF：不得静默当作空归档
    rec = _zero_block_truncated_record()
    r = post(rec)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "malformed_warc"
    assert d["record_order"] == 0
    assert d["record_id"] == "<urn:uuid:trunc>"
    assert d["declared_length"] == 100
    assert d["actual_length"] == 0


def test_zero_byte_truncated_record_after_complete_record():
    # 完整记录之后紧跟 0 字节截断记录：报错并给出第 1 条序号与该记录 ID，
    # 不能只返回前序记录。
    good = raw_record("<urn:uuid:ok>", block=http_block(body=b"fine"))
    bad = _zero_block_truncated_record(b"<urn:uuid:bad>")
    r = post(good + bad)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "malformed_warc"
    assert d["record_order"] == 1
    assert d["record_id"] == "<urn:uuid:bad>"
    assert d["actual_length"] == 0


def test_zero_byte_truncated_record_without_id_reports_order_only():
    rec = _zero_block_truncated_record(with_id=False)
    r = post(rec)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "malformed_warc"
    assert d["record_order"] == 0
    assert "record_id" not in d


def test_zero_byte_truncated_skipped_type_still_detected():
    # request 等被跳过的类型，0 字节截断同样不得被 warcio 静默丢弃
    rec = _zero_block_truncated_record(b"<urn:uuid:req>", rec_type=b"request")
    r = post(rec)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "malformed_warc"
    assert d["record_order"] == 0
    assert d["record_id"] == "<urn:uuid:req>"


def test_zero_byte_truncated_record_inside_single_stream_gzip():
    # 整条单流 gzip，解压后末记录 0 字节截断
    rec = _zero_block_truncated_record()
    r = post(gzip.compress(rec))
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "malformed_warc"
    assert d["record_id"] == "<urn:uuid:trunc>"


def test_zero_byte_truncated_record_inside_multimember_gzip():
    # 多 member gzip：完整记录一个 member，截断记录为最后一个 member
    good = raw_record("<urn:uuid:ok>", block=http_block(body=b"fine"))
    bad = _zero_block_truncated_record(b"<urn:uuid:bad>")
    multi_member = gzip.compress(good) + gzip.compress(bad)
    r = post(multi_member)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "malformed_warc"
    assert d["record_order"] == 1
    assert d["record_id"] == "<urn:uuid:bad>"


def test_truncated_unterminated_header_with_positive_length_detected():
    # WARC 头在 Content-Length 行之后未以空行结束即 EOF：块完全缺失
    rec = _zero_block_truncated_record().rstrip(b"\r\n")
    r = post(rec)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "malformed_warc"
    assert d["record_order"] == 0
    assert d["record_id"] == "<urn:uuid:trunc>"
    assert d["declared_length"] == 100
    assert "actual_length" not in d


def test_complete_record_without_trailing_separator_ok():
    # 块完整（恰好 Content-Length 字节）但没有记录末尾 \r\n\r\n，仍合法
    block = http_block(body=b"tail")
    rec = raw_record("<urn:uuid:ok>", block=block)[: -len(b"\r\n\r\n")]
    r = post(rec)
    assert r.status_code == 200, r.text
    assert base64.b64decode(r.json()["records"][0]["body_base64"]) == b"tail"


def test_consecutive_records_last_no_separator_ok():
    # 连续两条记录，最后一条块完整但无末尾分隔符，两条都应返回
    first = raw_record("<urn:uuid:a>", block=http_block(body=b"one"))
    block = http_block(body=b"two")
    second = raw_record("<urn:uuid:b>", block=block)[: -len(b"\r\n\r\n")]
    r = post(first + second)
    assert r.status_code == 200, r.text
    records = r.json()["records"]
    assert [x["record_id"] for x in records] == ["<urn:uuid:a>", "<urn:uuid:b>"]
    assert base64.b64decode(records[1]["body_base64"]) == b"two"


def test_multimember_gzip_multiple_records_still_ok():
    # 多 member gzip、每条记录一个 member，连续多记录正常处理
    recs = b"".join(
        gzip.compress(raw_record(f"<urn:uuid:{i}>", block=http_block(body=f"b{i}".encode())))
        for i in range(3)
    )
    r = post(recs)
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 3
    assert [x["record_id"] for x in r.json()["records"]] == [
        "<urn:uuid:0>", "<urn:uuid:1>", "<urn:uuid:2>"]


def test_malformed_warc_bad_field_line():
    bad = (b"WARC/1.1\r\nWARC-Type response\r\nWARC-Record-ID: <a>\r\n"
           b"Content-Length: 0\r\n\r\n\r\n\r\n")
    r = post(bad)
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"


def test_malformed_gzip():
    r = post(b"\x1f\x8bbroken gzip stream")
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"


def test_gzip_trailing_garbage():
    def fill(w):
        w.write_record(_writer_record(w, "<urn:uuid:a>", body=b"x"))
    data = gzip.compress(build_warc(fill)) + b"\x1f\x8bTAILJUNK"
    r = post(data)
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"


def test_invalid_base64():
    r = client.post("/v1/replay", json={"warc": "!!!not-base64!!!"})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_base64"


def test_invalid_base64_unicode():
    r = client.post("/v1/replay", json={"warc": "你好世界"})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_base64"


def test_missing_warc_field():
    r = client.post("/v1/replay", json={})
    assert r.status_code == 422


def test_decoded_size_limit():
    # 未压缩的 2 MiB + 1 字节数据，即使内容非法也要先拦体积
    blob = b"x" * (MAX_DECODED_BYTES + 1)
    r = client.post("/v1/replay", json={"warc": base64.b64encode(blob).decode()})
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "payload_too_large"
    assert d["decoded_size"] == MAX_DECODED_BYTES + 1
    assert d["max_bytes"] == MAX_DECODED_BYTES


def test_gzip_bomb_still_limited():
    # 解压后 4 MiB（超过 2 MiB 上限），压缩体本身很小
    co = zlib.compressobj(9, zlib.DEFLATED, 31)
    payload = b"0" * (4 * 1024 * 1024)
    data = co.compress(payload) + co.flush()
    assert len(data) < MAX_DECODED_BYTES  # 压缩体在限额内，解压后超限
    r = client.post("/v1/replay", json={"warc": base64.b64encode(data).decode()})
    assert r.status_code == 422
    assert r.json()["error"] == "payload_too_large"


def test_chain_points_to_revisit_without_refers_to():
    # v1 -> v2，v2 是无 Refers-To 的 revisit：链断裂
    rev1 = raw_record(
        "<urn:uuid:v1>", rec_type="revisit", block=b"",
        extra_headers=b"WARC-Refers-To: <urn:uuid:v2>\r\n")
    rev2 = raw_record("<urn:uuid:v2>", rec_type="revisit", block=b"")
    r = post(rev1 + rev2)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "chain_broken"
    assert d["record_id"] == "<urn:uuid:v1>"
    assert d["broken_record_id"] == "<urn:uuid:v2>"
    assert d["chain"] == ["<urn:uuid:v1>", "<urn:uuid:v2>"]


def test_body_preserved_raw_despite_content_encoding():
    # 正文按归档存储的原始字节返回，不解 Content-Encoding
    import gzip as _gzip
    from warcio.warcwriter import BufferWARCWriter
    from warcio.statusandheaders import StatusAndHeaders as SH
    payload = bytes(range(256))
    encoded = _gzip.compress(payload)
    w = BufferWARCWriter(gzip=False)
    w.write_record(w.create_warc_record(
        "http://bin/", "response",
        http_headers=SH("200 OK", [("Content-Type", "application/octet-stream"),
                                   ("Content-Encoding", "gzip"),
                                   ("Content-Length", str(len(encoded)))],
                        protocol="HTTP/1.1"),
        payload=io.BytesIO(encoded),
        warc_headers_dict={"WARC-Date": "d", "WARC-Record-ID": "<bin>"}))
    r = post(w.get_contents())
    assert r.status_code == 200
    body = base64.b64decode(r.json()["records"][0]["body_base64"])
    assert body == encoded  # 不是解包后的 payload


def test_ids_without_angle_brackets_supported():
    # WARC 规范要求尖括号，但宽松接受裸 URN
    rev = raw_record(
        "urn:uuid:rev", rec_type="revisit", block=b"",
        extra_headers=b"WARC-Refers-To: urn:uuid:a\r\n")
    resp = raw_record("urn:uuid:a", rec_type="response",
                      block=http_block(body=b"ok"))
    r = post(rev + resp)
    assert r.status_code == 200, r.text
    assert r.json()["records"][0]["body_source_id"] == "urn:uuid:a"
