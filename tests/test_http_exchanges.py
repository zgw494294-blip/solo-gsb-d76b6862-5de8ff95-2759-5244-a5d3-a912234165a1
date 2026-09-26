"""POST /v1/http-exchanges 端到端测试：显式 WARC-Concurrent-To 配对、
前向引用、未配对列出与五类配对错误。"""

from __future__ import annotations

import base64
import gzip

from fastapi.testclient import TestClient

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


def request_block(method="GET", target="/", headers=None, body=b""):
    return http_block(f"{method} {target} HTTP/1.1",
                      [("Host", "example.com")] + list(headers or []), body)


REQ_CT = b"Content-Type: application/http; msgtype=request\r\n"
RESP_CT = b"Content-Type: application/http; msgtype=response\r\n"


def request_record(rec_id, method="GET", target="/", extra_headers=b"",
                   uri="http://example.com/", **kw):
    return raw_record(rec_id, "request", request_block(method, target),
                      REQ_CT + extra_headers, uri=uri, **kw)


def response_record(rec_id, block=None, extra_headers=b"",
                    uri="http://example.com/", **kw):
    if block is None:
        block = http_block(body=b"hello")
    return raw_record(rec_id, "response", block, RESP_CT + extra_headers,
                      uri=uri, **kw)


def concurrent_to(*ids):
    return b"".join(
        f"WARC-Concurrent-To: {i}\r\n".encode() for i in ids
    )


def post(warc, gz=False):
    data = gzip.compress(warc) if gz else warc
    return client.post("/v1/http-exchanges",
                       json={"warc": base64.b64encode(data).decode()})


# ---------- 正常路径：显式关联配对 ----------


def test_pair_declared_by_request_side():
    q = request_record("<urn:uuid:q1>", "GET", "/home",
                       extra_headers=concurrent_to("<urn:uuid:s1>"))
    s = response_record("<urn:uuid:s1>")
    r = post(q + s)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 1
    assert data["pairs"] == [
        {
            "request_id": "<urn:uuid:q1>",
            "response_id": "<urn:uuid:s1>",
            "target_uri": "http://example.com/",
            "method": "GET",
            "request_target": "/home",
            "http_status": 200,
        }
    ]
    assert data["unpaired_requests"] == []
    assert data["unpaired_responses"] == []


def test_pair_declared_by_response_side():
    # 关联只出现在响应侧同样建立配对
    q = request_record("<urn:uuid:q1>", "POST", "/submit")
    s = response_record("<urn:uuid:s1>",
                        block=http_block("HTTP/1.1 201 Created", body=b"ok"),
                        extra_headers=concurrent_to("<urn:uuid:q1>"))
    r = post(q + s)
    assert r.status_code == 200, r.text
    pair = r.json()["pairs"][0]
    assert pair["request_id"] == "<urn:uuid:q1>"
    assert pair["response_id"] == "<urn:uuid:s1>"
    assert pair["method"] == "POST"
    assert pair["request_target"] == "/submit"
    assert pair["http_status"] == 201


def test_pair_mutual_declaration_ok():
    # 双方互指（声明一致）是合法配对，不算错误
    q = request_record("<urn:uuid:q1>",
                       extra_headers=concurrent_to("<urn:uuid:s1>"))
    s = response_record("<urn:uuid:s1>",
                        extra_headers=concurrent_to("<urn:uuid:q1>"))
    r = post(q + s)
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1


def test_forward_reference_allowed():
    # request 在前、response 在后：允许前向引用
    q = request_record("<urn:uuid:q1>",
                       extra_headers=concurrent_to("<urn:uuid:s1>"))
    s = response_record("<urn:uuid:s1>")
    r = post(q + s)
    assert r.status_code == 200, r.text
    assert r.json()["pairs"][0]["response_id"] == "<urn:uuid:s1>"


def test_id_normalization_bare_urn_and_whitespace():
    # 裸 URN、前后空白与 <...> 形式按同一规范化规则互通
    q = request_record("<urn:uuid:q1>",
                       extra_headers=b"WARC-Concurrent-To:  urn:uuid:s1  \r\n")
    s = response_record("urn:uuid:s1")
    r = post(q + s)
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1
    # 输出保留各自 WARC-Record-ID 原始值
    assert r.json()["pairs"][0]["response_id"] == "urn:uuid:s1"


def test_same_url_and_time_do_not_pair():
    # URL 与抓取时间都相同，但没有显式关联：不配对
    q = request_record("<urn:uuid:q1>")
    s = response_record("<urn:uuid:s1>")
    r = post(q + s)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 0
    assert data["unpaired_requests"] == ["<urn:uuid:q1>"]
    assert data["unpaired_responses"] == ["<urn:uuid:s1>"]


def test_pairs_ordered_by_request_archive_order():
    # 配对按 request 归档序，与 response 位置无关
    sb = response_record("<urn:uuid:sb>",
                         block=http_block("HTTP/1.1 204 No Content"),
                         uri="http://example.com/b")
    sa = response_record("<urn:uuid:sa>",
                         block=http_block("HTTP/1.1 404 Not Found", body=b"nf"),
                         uri="http://example.com/a")
    qb = request_record("<urn:uuid:qb>", "POST", "/b",
                        extra_headers=concurrent_to("<urn:uuid:sb>"),
                        uri="http://example.com/b")
    qa = request_record("<urn:uuid:qa>", "GET", "/a?x=1",
                        extra_headers=concurrent_to("<urn:uuid:sa>"),
                        uri="http://example.com/a")
    r = post(sb + sa + qb + qa)
    assert r.status_code == 200, r.text
    pairs = r.json()["pairs"]
    assert [p["request_id"] for p in pairs] == [
        "<urn:uuid:qb>", "<urn:uuid:qa>"]
    assert [(p["method"], p["request_target"], p["http_status"],
             p["target_uri"]) for p in pairs] == [
        ("POST", "/b", 204, "http://example.com/b"),
        ("GET", "/a?x=1", 404, "http://example.com/a"),
    ]


def test_request_target_absolute_form_preserved():
    q = request_record("<urn:uuid:q1>", "GET",
                       "http://example.com/abs?y=2",
                       extra_headers=concurrent_to("<urn:uuid:s1>"))
    s = response_record("<urn:uuid:s1>")
    r = post(q + s)
    assert r.status_code == 200, r.text
    assert r.json()["pairs"][0]["request_target"] == \
        "http://example.com/abs?y=2"


def test_revisit_as_response_side_without_http_headers():
    # revisit 作响应侧：自身无 HTTP 头时状态为 null
    a = response_record("<urn:uuid:a>")
    v = raw_record("<urn:uuid:v>", "revisit", b"",
                   b"WARC-Refers-To: <urn:uuid:a>\r\n"
                   + concurrent_to("<urn:uuid:q1>"),
                   uri="http://example.com/v")
    q = request_record("<urn:uuid:q1>", uri="http://example.com/v")
    r = post(a + v + q)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["pairs"][0]["response_id"] == "<urn:uuid:v>"
    assert data["pairs"][0]["http_status"] is None
    assert data["pairs"][0]["target_uri"] == "http://example.com/v"
    # 被 revisit 引用的 response 自身未配对，按原序列出
    assert data["unpaired_responses"] == ["<urn:uuid:a>"]


def test_revisit_as_response_side_with_own_status():
    a = response_record("<urn:uuid:a>")
    v = raw_record("<urn:uuid:v>", "revisit",
                   http_block("HTTP/1.1 301 Moved Permanently",
                              [("Location", "/new")]),
                   b"WARC-Refers-To: <urn:uuid:a>\r\n"
                   + concurrent_to("<urn:uuid:q1>"))
    q = request_record("<urn:uuid:q1>")
    r = post(a + v + q)
    assert r.status_code == 200, r.text
    assert r.json()["pairs"][0]["http_status"] == 301


def test_unpaired_records_listed_in_archive_order():
    q2 = request_record("<urn:uuid:q2>", uri="http://example.com/2")
    s1 = response_record("<urn:uuid:s1>", uri="http://example.com/1")
    q1 = request_record("<urn:uuid:q1>", uri="http://example.com/1",
                        extra_headers=concurrent_to("<urn:uuid:s2>"))
    s2 = response_record("<urn:uuid:s2>", uri="http://example.com/2")
    s3 = response_record("<urn:uuid:s3>", uri="http://example.com/3")
    r = post(q2 + s1 + q1 + s2 + s3)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 1
    assert data["unpaired_requests"] == ["<urn:uuid:q2>"]
    assert data["unpaired_responses"] == ["<urn:uuid:s1>", "<urn:uuid:s3>"]


def test_other_record_types_ignored():
    info = (
        b"WARC/1.1\r\nWARC-Type: warcinfo\r\n"
        b"WARC-Record-ID: <urn:uuid:info>\r\n"
        b"Content-Length: 4\r\n\r\ninfo\r\n\r\n"
    )
    q = request_record("<urn:uuid:q1>",
                       extra_headers=concurrent_to("<urn:uuid:s1>"))
    s = response_record("<urn:uuid:s1>")
    r = post(info + q + s)
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1


def test_empty_warc_returns_empty_lists():
    r = client.post("/v1/http-exchanges", json={"warc": ""})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "count": 0,
        "pairs": [],
        "unpaired_requests": [],
        "unpaired_responses": [],
    }


def test_gzip_input_accepted():
    q = request_record("<urn:uuid:q1>",
                       extra_headers=concurrent_to("<urn:uuid:s1>"))
    s = response_record("<urn:uuid:s1>")
    r = post(q + s, gz=True)
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1


# ---------- 配对错误：422 并指出相关 ID ----------


def test_multiple_partners_two_requests_claim_one_response():
    q1 = request_record("<urn:uuid:q1>",
                        extra_headers=concurrent_to("<urn:uuid:s>"))
    q2 = request_record("<urn:uuid:q2>",
                        extra_headers=concurrent_to("<urn:uuid:s>"))
    s = response_record("<urn:uuid:s>")
    r = post(q1 + q2 + s)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "multiple_partners"
    assert d["record_id"] == "<urn:uuid:s>"
    assert d["partners"] == ["<urn:uuid:q1>", "<urn:uuid:q2>"]


def test_multiple_partners_one_record_declares_two():
    q = request_record("<urn:uuid:q1>",
                       extra_headers=concurrent_to("<urn:uuid:s1>",
                                                   "<urn:uuid:s2>"))
    s1 = response_record("<urn:uuid:s1>")
    s2 = response_record("<urn:uuid:s2>")
    r = post(q + s1 + s2)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "multiple_partners"
    assert d["record_id"] == "<urn:uuid:q1>"
    assert d["partners"] == ["<urn:uuid:s1>", "<urn:uuid:s2>"]


def test_multiple_partners_inbound_and_outbound():
    # q 声明 s1，s2 又声明 q：s2 与 q 双方都有声明但指向不同伙伴，
    # 不一致错误先于多伙伴判定浮出水面
    q = request_record("<urn:uuid:q1>",
                       extra_headers=concurrent_to("<urn:uuid:s1>"))
    s1 = response_record("<urn:uuid:s1>")
    s2 = response_record("<urn:uuid:s2>",
                         extra_headers=concurrent_to("<urn:uuid:q1>"))
    r = post(q + s1 + s2)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "inconsistent_partners"
    assert d["record_id"] == "<urn:uuid:s2>"
    assert d["partner_id"] == "<urn:uuid:q1>"
    assert d["partner_points_to"] == "<urn:uuid:s1>"


def test_duplicate_concurrent_to_same_value_deduped():
    # 同一记录重复声明同一伙伴（同值）自动去重，不算多个伙伴
    q = request_record("<urn:uuid:q1>",
                       extra_headers=concurrent_to("<urn:uuid:s1>",
                                                   "<urn:uuid:s1>"))
    s = response_record("<urn:uuid:s1>")
    r = post(q + s)
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1


def test_inconsistent_partners_both_sides_point_elsewhere():
    # q 指向 s，而 s 指向 q2：双方指向不同伙伴
    q = request_record("<urn:uuid:q1>",
                       extra_headers=concurrent_to("<urn:uuid:s1>"))
    s = response_record("<urn:uuid:s1>",
                        extra_headers=concurrent_to("<urn:uuid:q2>"))
    q2 = request_record("<urn:uuid:q2>")
    r = post(q + s + q2)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "inconsistent_partners"
    assert d["record_id"] == "<urn:uuid:q1>"
    assert d["partner_id"] == "<urn:uuid:s1>"
    assert d["partner_points_to"] == "<urn:uuid:q2>"


def test_concurrent_target_not_found():
    q = request_record("<urn:uuid:q1>",
                       extra_headers=concurrent_to("<urn:uuid:nope>"))
    r = post(q)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "concurrent_target_not_found"
    assert d["record_id"] == "<urn:uuid:q1>"
    assert d["missing_id"] == "<urn:uuid:nope>"


def test_concurrent_target_not_found_for_skipped_types():
    # warcinfo 等类型不参与配对：指向其 ID 等同于目标不存在
    info = (
        b"WARC/1.1\r\nWARC-Type: warcinfo\r\n"
        b"WARC-Record-ID: <urn:uuid:info>\r\n"
        b"Content-Length: 4\r\n\r\ninfo\r\n\r\n"
    )
    q = request_record("<urn:uuid:q1>",
                       extra_headers=concurrent_to("<urn:uuid:info>"))
    r = post(info + q)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "concurrent_target_not_found"
    assert d["missing_id"] == "<urn:uuid:info>"


def test_concurrent_type_mismatch_request_to_request():
    q1 = request_record("<urn:uuid:q1>",
                        extra_headers=concurrent_to("<urn:uuid:q2>"))
    q2 = request_record("<urn:uuid:q2>")
    r = post(q1 + q2)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "concurrent_type_mismatch"
    assert d["record_id"] == "<urn:uuid:q1>"
    assert d["record_type"] == "request"
    assert d["target_id"] == "<urn:uuid:q2>"
    assert d["target_type"] == "request"


def test_concurrent_type_mismatch_response_to_response():
    s1 = response_record("<urn:uuid:s1>",
                         extra_headers=concurrent_to("<urn:uuid:s2>"))
    s2 = response_record("<urn:uuid:s2>")
    r = post(s1 + s2)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "concurrent_type_mismatch"
    assert d["target_type"] == "response"


def test_concurrent_type_mismatch_response_to_revisit():
    a = response_record("<urn:uuid:a>")
    v = raw_record("<urn:uuid:v>", "revisit", b"",
                   b"WARC-Refers-To: <urn:uuid:a>\r\n")
    s = response_record("<urn:uuid:s>",
                        extra_headers=concurrent_to("<urn:uuid:v>"))
    r = post(a + v + s)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "concurrent_type_mismatch"
    assert d["target_id"] == "<urn:uuid:v>"
    assert d["target_type"] == "revisit"


def test_duplicate_record_id_request_vs_response():
    q = request_record("<urn:uuid:x>")
    s = response_record("<urn:uuid:x>")
    r = post(q + s)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "duplicate_record_id"
    assert d["record_id"] == "<urn:uuid:x>"
    assert d["first_record_order"] == 0
    assert d["duplicate_record_order"] == 1


def test_duplicate_record_id_between_requests():
    q1 = request_record("<urn:uuid:x>")
    q2 = request_record("<urn:uuid:x>")
    r = post(q1 + q2)
    assert r.status_code == 422
    assert r.json()["error"] == "duplicate_record_id"


def test_missing_record_id_on_request():
    warc = (
        b"WARC/1.1\r\nWARC-Type: request\r\n"
        b"WARC-Date: 2020-01-01T00:00:00Z\r\n"
        b"WARC-Target-URI: http://example.com/\r\n"
        + REQ_CT
        + f"Content-Length: {len(request_block())}\r\n".encode()
        + b"\r\n" + request_block() + b"\r\n\r\n"
    )
    r = post(warc)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "missing_record_id"
    assert d["record_type"] == "request"


def test_request_without_http_headers():
    q = raw_record("<urn:uuid:q1>", "request", b"", REQ_CT)
    r = post(q)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "request_without_http_headers"
    assert d["record_id"] == "<urn:uuid:q1>"


def test_request_with_response_status_line_rejected():
    # request 记录的块是 HTTP 响应（状态行而非请求行）
    q = raw_record("<urn:uuid:q1>", "request", http_block(body=b"x"), REQ_CT)
    r = post(q)
    assert r.status_code == 422
    assert r.json()["error"] == "request_without_http_headers"


# ---------- 沿用现有校验 ----------


def test_revisit_chain_validation_still_applies():
    v = raw_record("<urn:uuid:v>", "revisit", b"",
                   b"WARC-Refers-To: <urn:uuid:ghost>\r\n")
    r = post(v)
    assert r.status_code == 422
    assert r.json()["error"] == "target_not_found"


def test_response_without_http_headers_still_applies():
    s = raw_record("<urn:uuid:s>", "response", b"", RESP_CT)
    r = post(s)
    assert r.status_code == 422
    assert r.json()["error"] == "response_without_http_headers"


def test_invalid_base64():
    r = client.post("/v1/http-exchanges", json={"warc": "@@@"})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_base64"


def test_payload_too_large():
    big = b"WARC/1.0\r\n" + b"x" * (MAX_DECODED_BYTES + 1)
    r = post(big)
    assert r.status_code == 422
    assert r.json()["error"] == "payload_too_large"


def test_malformed_warc():
    r = post(b"not a warc at all")
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"


def test_truncated_record_block():
    block = http_block(body=b"hello")
    warc = (
        b"WARC/1.1\r\nWARC-Type: response\r\n"
        b"WARC-Record-ID: <urn:uuid:s>\r\n"
        b"WARC-Date: 2020-01-01T00:00:00Z\r\n"
        b"WARC-Target-URI: http://example.com/\r\n"
        b"Content-Length: 9999\r\n\r\n" + block
    )
    r = post(warc)
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"


def test_request_missing_target_uri_is_malformed():
    # 与 response 一致：缺 WARC-Target-URI 的 HTTP 记录判畸形
    block = request_block()
    warc = (
        b"WARC/1.1\r\nWARC-Type: request\r\n"
        b"WARC-Record-ID: <urn:uuid:q1>\r\n"
        b"WARC-Date: 2020-01-01T00:00:00Z\r\n"
        + REQ_CT
        + f"Content-Length: {len(block)}\r\n".encode()
        + b"\r\n" + block + b"\r\n\r\n"
    )
    r = post(warc)
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"
