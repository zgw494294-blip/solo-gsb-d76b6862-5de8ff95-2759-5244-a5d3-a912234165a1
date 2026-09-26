"""/v1/navigate 端到端测试：时点选取、重定向跟随、revisit 与错误场景。"""

from __future__ import annotations

import base64
import io

import pytest
from fastapi.testclient import TestClient
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import BufferWARCWriter

from app.main import app
from app.navigate import MAX_HOPS
from app.replay import MAX_DECODED_BYTES

client = TestClient(app)


# ---------- WARC 构造辅助 ----------


def _make_response(w, rec_id, uri, date, status_code=200, reason="OK",
                   body=b"", location=None, with_http=True, rec_type="response",
                   refers_to=None):
    headers = []
    if with_http:
        if location is not None:
            headers.append(("Location", location))
        headers.append(("Content-Length", str(len(body))))
        http = StatusAndHeaders(f"{status_code} {reason}", headers,
                                protocol="HTTP/1.1")
    else:
        http = None
    wh = {"WARC-Date": date, "WARC-Record-ID": rec_id}
    if refers_to is not None:
        wh["WARC-Refers-To"] = refers_to
    return w.create_warc_record(
        uri, rec_type, http_headers=http, payload=io.BytesIO(body),
        warc_headers_dict=wh,
    )


def build_warc(fn, gzip_=False):
    w = BufferWARCWriter(gzip=gzip_)
    fn(w)
    return w.get_contents()


def raw_record(rec_id="<urn:uuid:r>", rec_type="response", block=b"",
               extra_headers=b"", date="2020-01-01T00:00:00Z",
               uri="http://example.com/"):
    uri_line = f"WARC-Target-URI: {uri}\r\n".encode() if uri is not None else b""
    return (
        f"WARC/1.1\r\nWARC-Type: {rec_type}\r\n"
        f"WARC-Record-ID: {rec_id}\r\nWARC-Date: {date}\r\n".encode()
        + uri_line
        + f"Content-Length: {len(block)}\r\n".encode()
        + extra_headers + b"\r\n" + block + b"\r\n\r\n"
    )


def http_block(status_line="HTTP/1.1 200 OK", headers=None, body=b""):
    h = status_line.encode() + b"\r\n"
    for k, v in (headers or []):
        h += f"{k}: {v}\r\n".encode()
    return h + b"\r\n" + body


def nav(warc_bytes, url, at, **kw):
    return client.post(
        "/v1/navigate",
        json={"warc": base64.b64encode(warc_bytes).decode(), "url": url, "at": at},
        **kw,
    )


def nav_b64(warc_b64, url, at, **kw):
    return client.post("/v1/navigate", json={"warc": warc_b64, "url": url,
                                             "at": at}, **kw)


# ---------- 正常路径：时点选取 ----------


def test_selects_latest_not_after_point_in_time():
    def fill(w):
        w.write_record(_make_response(w, "<urn:uuid:1>", "http://a/",
                                      "2020-01-01T00:00:00Z", body=b"v1"))
        w.write_record(_make_response(w, "<urn:uuid:2>", "http://a/",
                                      "2021-01-01T00:00:00Z", body=b"v2"))
        w.write_record(_make_response(w, "<urn:uuid:3>", "http://a/",
                                      "2022-01-01T00:00:00Z", body=b"v3"))
    r = nav(build_warc(fill), "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["final_url"] == "http://a/"
    assert d["body_source_id"] == "<urn:uuid:2>"
    assert base64.b64decode(d["body_base64"]) == b"v2"
    assert len(d["path"]) == 1
    hop = d["path"][0]
    assert hop["url"] == "http://a/"
    assert hop["record_id"] == "<urn:uuid:2>"
    assert hop["capture_time"] == "2021-01-01T00:00:00Z"
    assert hop["http_status"] == 200


def test_capture_at_exactly_point_in_time_is_eligible():
    def fill(w):
        w.write_record(_make_response(w, "<urn:uuid:1>", "http://a/",
                                      "2021-01-01T00:00:00Z", body=b"exact"))
    r = nav(build_warc(fill), "http://a/", "2021-01-01T00:00:00Z")
    assert r.status_code == 200
    assert r.json()["body_source_id"] == "<urn:uuid:1>"


def test_same_timestamp_uses_later_archive_order():
    def fill(w):
        w.write_record(_make_response(w, "<urn:uuid:earlier>", "http://a/",
                                      "2021-01-01T00:00:00Z", body=b"first"))
        w.write_record(_make_response(w, "<urn:uuid:later>", "http://a/",
                                      "2021-01-01T00:00:00Z", body=b"second"))
    r = nav(build_warc(fill), "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 200, r.text
    assert r.json()["body_source_id"] == "<urn:uuid:later>"
    assert base64.b64decode(r.json()["body_base64"]) == b"second"


def test_all_captures_after_point_in_time_is_404():
    def fill(w):
        w.write_record(_make_response(w, "<urn:uuid:1>", "http://a/",
                                      "2022-01-01T00:00:00Z", body=b"future"))
    r = nav(build_warc(fill), "http://a/", "2020-01-01T00:00:00Z")
    assert r.status_code == 404
    d = r.json()
    assert d["error"] == "capture_not_found"
    assert d["url"] == "http://a/"
    assert d["at"] == "2020-01-01T00:00:00Z"
    assert d["path"] == []


def test_records_without_url_or_date_excluded():
    # 无 URL 与无日期的记录不参与选择；有合法同 URL 记录时正常返回。
    # 无 URL 的 revisit（warcio 对无 URI 记录不解析 HTTP 头）指向合法终点，
    # 仍应因缺 URL 而被排除。
    origin = raw_record("<urn:uuid:origin>", uri="http://a/origin",
                        block=http_block(body=b"origin"))
    no_url = raw_record(
        "<urn:uuid:nourl>", rec_type="revisit", uri=None, block=b"",
        extra_headers=b"WARC-Refers-To: <urn:uuid:origin>\r\n")
    no_date_block = http_block(body=b"")
    no_date = (
        b"WARC/1.1\r\nWARC-Type: response\r\nWARC-Record-ID: <urn:uuid:nodate>\r\n"
        b"WARC-Target-URI: http://a/\r\n"
        b"Content-Length: " + str(len(no_date_block)).encode() + b"\r\n\r\n"
        + no_date_block + b"\r\n\r\n"
    )
    good = raw_record("<urn:uuid:good>", uri="http://a/",
                      block=http_block(body=b"good"))
    r = nav(origin + no_url + no_date + good, "http://a/",
            "2021-01-01T00:00:00Z")
    assert r.status_code == 200, r.text
    assert r.json()["body_source_id"] == "<urn:uuid:good>"


@pytest.mark.parametrize("order", ["before", "after"])
def test_response_without_target_uri_does_not_break_navigate(order):
    # 结构完整但缺 WARC-Target-URI 的 response（带有效 HTTP 头与正文）曾让
    # warcio 抛 AttributeError、整包被误判 malformed_warc；它不参与时点选择，
    # 起始 URL 的有效抓取必须正常返回路径、正文与来源 ID（无论归档先后）。
    no_url = raw_record("<urn:uuid:nouri>", uri=None,
                        block=http_block(body=b"orphan"))
    good = raw_record("<urn:uuid:good>", uri="http://a/",
                      block=http_block(body=b"good"))
    warc = no_url + good if order == "before" else good + no_url
    r = nav(warc, "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["final_url"] == "http://a/"
    assert base64.b64decode(d["body_base64"]) == b"good"
    assert d["body_source_id"] == "<urn:uuid:good>"
    assert [h["record_id"] for h in d["path"]] == ["<urn:uuid:good>"]
    assert d["path"][0]["capture_time"] == "2020-01-01T00:00:00Z"
    assert d["path"][0]["http_status"] == 200


def test_only_url_less_capture_is_404():
    # 缺 URL 的记录绝不能顶替起始 URL 的抓取：包内只有它时仍为 404
    no_url = raw_record("<urn:uuid:nouri>", uri=None,
                        block=http_block(body=b"orphan"))
    r = nav(no_url, "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 404
    d = r.json()
    assert d["error"] == "capture_not_found"
    assert d["url"] == "http://a/"
    assert d["path"] == []


def test_url_less_record_tolerated_under_multimember_gzip():
    import gzip
    no_url = raw_record("<urn:uuid:nouri>", uri=None,
                        block=http_block(body=b"orphan"))
    good = raw_record("<urn:uuid:good>", uri="http://a/",
                      block=http_block(body=b"good"))
    r = nav(gzip.compress(no_url) + gzip.compress(good),
            "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 200, r.text
    assert base64.b64decode(r.json()["body_base64"]) == b"good"


def test_truncated_url_less_record_still_malformed():
    # 缺 URL 的末记录块截断（头完整、Content-Length 为正、头后直接 EOF）：
    # 不得被容忍，预扫仍判 malformed_warc 并给出序号与记录 ID
    good = raw_record("<urn:uuid:good>", uri="http://a/",
                      block=http_block(body=b"good"))
    bad = (
        b"WARC/1.1\r\nWARC-Type: response\r\n"
        b"WARC-Record-ID: <urn:uuid:nouritrunc>\r\n"
        b"WARC-Date: 2020-01-01T00:00:00Z\r\n"
        b"Content-Length: 100\r\n\r\n"
    )
    r = nav(good + bad, "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "malformed_warc"
    assert d["record_order"] == 1
    assert d["record_id"] == "<urn:uuid:nouritrunc>"
    assert d["declared_length"] == 100
    assert d["actual_length"] == 0


def test_url_less_record_missing_id_still_rejected():
    # 缺 URL 且缺 WARC-Record-ID：missing_record_id 仍须拦截
    block = http_block(body=b"x")
    no_id = (
        b"WARC/1.1\r\nWARC-Type: response\r\n"
        b"WARC-Date: 2020-01-01T00:00:00Z\r\n"
        b"Content-Length: " + str(len(block)).encode() + b"\r\n\r\n" + block
        + b"\r\n\r\n"
    )
    good = raw_record("<urn:uuid:good>", uri="http://a/",
                      block=http_block(body=b"good"))
    r = nav(no_id + good, "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "missing_record_id"
    assert d["record_type"] == "response"


def test_duplicate_id_involving_url_less_record_still_rejected():
    # 缺 URL 记录与其他记录 ID 重复：duplicate_record_id 仍须拦截
    dup_no_url = raw_record("<urn:uuid:dup>", uri=None,
                            block=http_block(body=b"x"))
    dup_other = raw_record("<urn:uuid:dup>", uri="http://other/",
                           block=http_block(body=b"y"))
    good = raw_record("<urn:uuid:good>", uri="http://a/",
                      block=http_block(body=b"good"))
    r = nav(dup_no_url + dup_other + good, "http://a/",
            "2021-06-01T00:00:00Z")
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "duplicate_record_id"
    assert d["record_id"] == "<urn:uuid:dup>"


def test_url_less_revisit_broken_chain_still_422():
    # 缺 URL 的 revisit（且带自己的 HTTP 块）断链：引用校验照常 422，
    # 不能因记录不参与选择而跳过 revisit 引用链检查
    rev = raw_record(
        "<urn:uuid:rev>", rec_type="revisit", uri=None,
        block=http_block("HTTP/1.1 200 OK", body=b""),
        extra_headers=b"WARC-Refers-To: <urn:uuid:ghost>\r\n")
    good = raw_record("<urn:uuid:good>", uri="http://a/",
                      block=http_block(body=b"good"))
    r = nav(rev + good, "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "target_not_found"
    assert d["record_id"] == "<urn:uuid:rev>"
    assert d["missing_id"] == "<urn:uuid:ghost>"


def test_url_less_response_without_http_headers_still_422():
    # 缺 URL 的 response 自身没有有效 HTTP 响应头：统一结构校验仍判 422
    no_url = raw_record("<urn:uuid:nouri>", uri=None, block=b"")
    good = raw_record("<urn:uuid:good>", uri="http://a/",
                      block=http_block(body=b"good"))
    r = nav(no_url + good, "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 422
    assert r.json()["error"] == "response_without_http_headers"


def test_url_match_is_exact_no_normalization():
    def fill(w):
        w.write_record(_make_response(w, "<urn:uuid:1>", "http://a/",
                                      "2021-01-01T00:00:00Z", body=b"slash"))
    data = build_warc(fill)
    assert nav(data, "http://a", "2021-06-01T00:00:00Z").status_code == 404
    assert nav(data, "http://A/", "2021-06-01T00:00:00Z").status_code == 404
    r = nav(data, "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 200


def test_non_redirect_status_is_terminal():
    def fill(w):
        w.write_record(_make_response(w, "<urn:uuid:1>", "http://a/",
                                      "2021-01-01T00:00:00Z",
                                      status_code=200, body=b"ok"))
        # 更新的 404 也应作为终点返回（接口本身仍 200）
        w.write_record(_make_response(w, "<urn:uuid:2>", "http://a/",
                                      "2021-05-01T00:00:00Z",
                                      status_code=404, reason="Not Found",
                                      body=b"nf"))
    r = nav(build_warc(fill), "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 200
    d = r.json()
    assert d["path"][-1]["http_status"] == 404
    assert base64.b64decode(d["body_base64"]) == b"nf"


def test_empty_body_terminal():
    def fill(w):
        w.write_record(_make_response(w, "<urn:uuid:1>", "http://a/",
                                      "2021-01-01T00:00:00Z", body=b""))
    r = nav(build_warc(fill), "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 200
    assert r.json()["body_base64"] == ""


def test_offset_timezone_normalized_to_utc():
    def fill(w):
        # 2021-01-01T02:00:00Z
        w.write_record(_make_response(w, "<urn:uuid:1>", "http://a/",
                                      "2021-01-01T02:00:00Z", body=b"z"))
        w.write_record(_make_response(w, "<urn:uuid:2>", "http://a/",
                                      "2021-01-01T05:00:00Z", body=b"later"))
    data = build_warc(fill)
    # 04:00+02:00 == 02:00Z：恰好等于第一条，第二条在未来
    r = nav(data, "http://a/", "2021-01-01T04:00:00+02:00")
    assert r.status_code == 200
    assert r.json()["body_source_id"] == "<urn:uuid:1>"


# ---------- 重定向 ----------


def _redirect_chain_warc():
    def fill(w):
        w.write_record(_make_response(
            w, "<urn:uuid:a1>", "http://a/start", "2021-01-01T00:00:00Z",
            status_code=301, reason="Moved Permanently", location="/next"))
        w.write_record(_make_response(
            w, "<urn:uuid:a2>", "http://a/next", "2021-02-01T00:00:00Z",
            status_code=302, reason="Found",
            location="http://a/final?q=1"))
        w.write_record(_make_response(
            w, "<urn:uuid:a3>", "http://a/final?q=1", "2021-03-01T00:00:00Z",
            body=b"done"))
    return build_warc(fill)


def test_redirect_chain_relative_and_absolute():
    r = nav(_redirect_chain_warc(), "http://a/start", "2021-06-01T00:00:00Z")
    assert r.status_code == 200, r.text
    d = r.json()
    assert [h["record_id"] for h in d["path"]] == [
        "<urn:uuid:a1>", "<urn:uuid:a2>", "<urn:uuid:a3>"]
    assert [h["url"] for h in d["path"]] == [
        "http://a/start", "http://a/next", "http://a/final?q=1"]
    assert [h["http_status"] for h in d["path"]] == [301, 302, 200]
    assert d["final_url"] == "http://a/final?q=1"
    assert base64.b64decode(d["body_base64"]) == b"done"
    assert d["body_source_id"] == "<urn:uuid:a3>"


def test_redirect_relative_resolution_forms():
    # 相对 Location 按 RFC3986 相对当前 URL 解析
    def fill(w):
        w.write_record(_make_response(
            w, "<urn:uuid:1>", "http://a/x/y/page", "2021-01-01T00:00:00Z",
            status_code=307, reason="Temporary Redirect", location="other"))
        w.write_record(_make_response(
            w, "<urn:uuid:2>", "http://a/x/y/other", "2021-02-01T00:00:00Z",
            body=b"rel"))
    r = nav(build_warc(fill), "http://a/x/y/page", "2021-06-01T00:00:00Z")
    assert r.status_code == 200
    assert r.json()["final_url"] == "http://a/x/y/other"


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_all_redirect_statuses_followed(code, reason="Redirect"):
    def fill(w):
        w.write_record(_make_response(
            w, "<urn:uuid:1>", "http://a/", "2021-01-01T00:00:00Z",
            status_code=code, reason=reason, location="/b"))
        w.write_record(_make_response(
            w, "<urn:uuid:2>", "http://a/b", "2021-02-01T00:00:00Z",
            body=b"b"))
    r = nav(build_warc(fill), "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 200, r.text
    assert r.json()["final_url"] == "http://a/b"


def test_redirect_target_selection_also_time_bounded():
    # 第二跳在时点之后也不可见 → 404，且 path 报告已走的第一跳
    def fill(w):
        w.write_record(_make_response(
            w, "<urn:uuid:1>", "http://a/", "2021-01-01T00:00:00Z",
            status_code=301, location="/b"))
        w.write_record(_make_response(
            w, "<urn:uuid:2>", "http://a/b", "2022-01-01T00:00:00Z",
            body=b"future-b"))
    r = nav(build_warc(fill), "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 404
    d = r.json()
    assert d["error"] == "capture_not_found"
    assert d["url"] == "http://a/b"
    assert [h["record_id"] for h in d["path"]] == ["<urn:uuid:1>"]


def test_redirect_chain_seen_per_point_in_time():
    # 同一 URL 上旧的 200 与新的 301：早时点直接终止，晚时点跟随
    def fill(w):
        w.write_record(_make_response(
            w, "<urn:uuid:old200>", "http://a/", "2020-01-01T00:00:00Z",
            body=b"old"))
        w.write_record(_make_response(
            w, "<urn:uuid:new301>", "http://a/", "2021-01-01T00:00:00Z",
            status_code=301, location="/b"))
        w.write_record(_make_response(
            w, "<urn:uuid:b>", "http://a/b", "2021-02-01T00:00:00Z",
            body=b"b"))
    data = build_warc(fill)
    r1 = nav(data, "http://a/", "2020-06-01T00:00:00Z")
    assert r1.status_code == 200
    assert r1.json()["body_source_id"] == "<urn:uuid:old200>"
    r2 = nav(data, "http://a/", "2021-06-01T00:00:00Z")
    assert r2.status_code == 200
    assert r2.json()["final_url"] == "http://a/b"


# ---------- revisit ----------


def test_revisit_terminal_inherits_endpoint_body_only():
    def fill(w):
        w.write_record(_make_response(
            w, "<urn:uuid:orig>", "http://a/orig", "2020-01-01T00:00:00Z",
            body=b"ORIGINAL-BODY"))
        w.write_record(_make_response(
            w, "<urn:uuid:rev>", "http://a/", "2021-01-01T00:00:00Z",
            rec_type="revisit", with_http=False, refers_to="<urn:uuid:orig>"))
    r = nav(build_warc(fill), "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 200, r.text
    d = r.json()
    assert base64.b64decode(d["body_base64"]) == b"ORIGINAL-BODY"
    assert d["body_source_id"] == "<urn:uuid:orig>"
    assert d["path"][0]["record_id"] == "<urn:uuid:rev>"
    assert d["path"][0]["http_status"] is None
    assert d["path"][0]["capture_time"] == "2021-01-01T00:00:00Z"
    assert d["final_url"] == "http://a/"


def test_revisit_redirect_uses_own_location_not_endpoints():
    # revisit 自己是 301 且有自己的 Location；链终点 response 也带 Location，
    # 但 navigate 只能采用 revisit 自己的，不得借用终点的。
    endpoint = raw_record(
        "<urn:uuid:orig>", rec_type="response", uri="http://a/orig",
        date="2020-01-01T00:00:00Z",
        block=http_block("HTTP/1.1 302 Found",
                         headers=[("Location", "http://evil/endpoint-loc")],
                         body=b""))
    rev_block = http_block("HTTP/1.1 301 Moved",
                           headers=[("Location", "/own-target")], body=b"")
    revisit = raw_record(
        "<urn:uuid:rev>", rec_type="revisit", uri="http://a/",
        date="2021-01-01T00:00:00Z", block=rev_block,
        extra_headers=b"WARC-Refers-To: <urn:uuid:orig>\r\n")
    target = raw_record(
        "<urn:uuid:t>", rec_type="response", uri="http://a/own-target",
        date="2021-02-01T00:00:00Z", block=http_block(body=b"own"))
    r = nav(endpoint + revisit + target, "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 200, r.text
    d = r.json()
    assert [h["url"] for h in d["path"]] == ["http://a/", "http://a/own-target"]
    assert d["path"][0]["http_status"] == 301
    assert d["final_url"] == "http://a/own-target"
    assert base64.b64decode(d["body_base64"]) == b"own"


def test_revisit_redirect_body_comes_from_chain_endpoint():
    # revisit 301 -> 第二跳也是 revisit 链场景之外：确认第一跳的 body 不影响，
    # 最终终为 response；这里仅验证第一跳 revisit 正文已被还原（虽不返回）。
    def fill(w):
        w.write_record(_make_response(
            w, "<urn:uuid:orig>", "http://a/orig", "2020-01-01T00:00:00Z",
            body=b"INHERITED"))
        w.write_record(_make_response(
            w, "<urn:uuid:rev>", "http://a/", "2021-01-01T00:00:00Z",
            status_code=301, reason="Moved", location="/b", body=b"",
            rec_type="revisit", refers_to="<urn:uuid:orig>"))
        w.write_record(_make_response(
            w, "<urn:uuid:b>", "http://a/b", "2021-02-01T00:00:00Z",
            body=b"BODY-B"))
    r = nav(build_warc(fill), "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 200
    assert r.json()["body_source_id"] == "<urn:uuid:b>"


def test_revisit_redirect_chain_endpoint_body_at_terminal():
    # 跟随后的终点本身是 revisit：只继承终点正文，不借用终点 response 的状态
    def fill(w):
        w.write_record(_make_response(
            w, "<urn:uuid:redir>", "http://a/", "2021-01-01T00:00:00Z",
            status_code=302, location="/b"))
        w.write_record(_make_response(
            w, "<urn:uuid:orig>", "http://a/orig", "2020-01-01T00:00:00Z",
            body=b"ORIG"))
        w.write_record(_make_response(
            w, "<urn:uuid:revb>", "http://a/b", "2021-03-01T00:00:00Z",
            rec_type="revisit", with_http=False, refers_to="<urn:uuid:orig>"))
    r = nav(build_warc(fill), "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["final_url"] == "http://a/b"
    assert base64.b64decode(d["body_base64"]) == b"ORIG"
    assert d["body_source_id"] == "<urn:uuid:orig>"
    assert d["path"][1]["http_status"] is None


# ---------- 422：重定向异常 ----------


def test_missing_location_returns_422_and_path():
    block = http_block("HTTP/1.1 302 Found", body=b"")
    rec = raw_record("<urn:uuid:1>", uri="http://a/", block=block)
    r = nav(rec, "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "missing_location"
    assert d["record_id"] == "<urn:uuid:1>"
    assert d["http_status"] == 302
    assert [h["record_id"] for h in d["path"]] == ["<urn:uuid:1>"]


def test_blank_location_treated_as_missing():
    block = http_block("HTTP/1.1 301 Moved",
                       headers=[("Location", "   ")], body=b"")
    rec = raw_record("<urn:uuid:1>", uri="http://a/", block=block)
    r = nav(rec, "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 422
    assert r.json()["error"] == "missing_location"


def test_non_http_location_scheme_422():
    block = http_block("HTTP/1.1 302 Found",
                       headers=[("Location", "mailto:a@b.com")], body=b"")
    rec = raw_record("<urn:uuid:1>", uri="http://a/", block=block)
    r = nav(rec, "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "invalid_redirect_target"
    assert d["resolved_url"] == "mailto:a@b.com"
    assert d["path"]


def test_redirect_cycle_422():
    def fill(w):
        w.write_record(_make_response(
            w, "<urn:uuid:1>", "http://a/one", "2021-01-01T00:00:00Z",
            status_code=301, location="/two"))
        w.write_record(_make_response(
            w, "<urn:uuid:2>", "http://a/two", "2021-02-01T00:00:00Z",
            status_code=302, location="/one"))
    r = nav(build_warc(fill), "http://a/one", "2021-06-01T00:00:00Z")
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "redirect_cycle"
    assert d["url"] == "http://a/one"
    # 成环时路径闭合到重复的 URL
    assert [h["url"] for h in d["path"]] == [
        "http://a/one", "http://a/two", "http://a/one"]


def test_self_redirect_cycle_422():
    def fill(w):
        w.write_record(_make_response(
            w, "<urn:uuid:1>", "http://a/self", "2021-01-01T00:00:00Z",
            status_code=301, location="#frag"))
    # Location 为纯片段时 URL 去掉片段后仍是同一 http(s) 目标
    r = nav(build_warc(fill), "http://a/self", "2021-06-01T00:00:00Z")
    assert r.status_code == 422
    assert r.json()["error"] == "redirect_cycle"


def test_too_many_redirects_422():
    # MAX_HOPS 条记录全是重定向（彼此串联）：选出第 MAX_HOPS 条后再需一跳
    def fill(w):
        for i in range(MAX_HOPS + 2):
            w.write_record(_make_response(
                w, f"<urn:uuid:{i}>", f"http://a/h{i}",
                "2021-01-01T00:00:00Z",
                status_code=302, location=f"/h{i + 1}"))
    r = nav(build_warc(fill), "http://a/h0", "2021-06-01T00:00:00Z")
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "too_many_redirects"
    assert d["max_hops"] == MAX_HOPS
    assert d["next_url"] == f"http://a/h{MAX_HOPS}"
    assert len(d["path"]) == MAX_HOPS
    assert [h["url"] for h in d["path"]] == [
        f"http://a/h{i}" for i in range(MAX_HOPS)]


def test_exactly_max_hops_with_terminal_is_ok():
    # MAX_HOPS-1 次重定向 + 终点，恰好 MAX_HOPS 跳：成功
    def fill(w):
        for i in range(MAX_HOPS - 1):
            w.write_record(_make_response(
                w, f"<urn:uuid:{i}>", f"http://a/h{i}",
                "2021-01-01T00:00:00Z",
                status_code=302, location=f"/h{i + 1}"))
        w.write_record(_make_response(
            w, f"<urn:uuid:end>", f"http://a/h{MAX_HOPS - 1}",
            "2021-01-01T00:00:00Z", body=b"end"))
    r = nav(build_warc(fill), "http://a/h0", "2021-06-01T00:00:00Z")
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["path"]) == MAX_HOPS
    assert d["final_url"] == f"http://a/h{MAX_HOPS - 1}"


# ---------- 422：入参与 WARC ----------


def test_invalid_start_url_422():
    warc_b64 = base64.b64encode(b"").decode()
    for bad in ("ftp://a/", "not a url", "http://", ""):
        r = nav_b64(warc_b64, bad, "2021-01-01T00:00:00Z")
        assert r.status_code == 422, bad
        assert r.json()["error"] == "invalid_start_url"


@pytest.mark.parametrize("bad_at", [
    "2021-13-01T00:00:00Z",       # 非法月份
    "2021/01/01 00:00:00",        # 非 RFC3339 分隔
    "2021-01-01",                 # 仅日期
    "2021-01-01T00:00:00",        # 无时区
    "not-a-time",
])
def test_invalid_at_422(bad_at):
    r = nav_b64(base64.b64encode(b"").decode(), "http://a/", bad_at)
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_at"


def test_missing_json_fields_422():
    r = client.post("/v1/navigate", json={"warc": ""})
    assert r.status_code == 422


def test_invalid_capture_date_in_warc_422():
    rec = raw_record("<urn:uuid:1>", uri="http://a/", date="not-a-date",
                     block=http_block(body=b"x"))
    r = nav(rec, "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "invalid_capture_date"
    assert d["record_id"] == "<urn:uuid:1>"
    assert d["warc_date"] == "not-a-date"


def test_malformed_warc_still_422_via_shared_validation():
    r = client.post("/v1/navigate",
                    json={"warc": base64.b64encode(b"garbage").decode(),
                          "url": "http://a/", "at": "2021-01-01T00:00:00Z"})
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"


def test_invalid_base64_still_422():
    r = client.post("/v1/navigate",
                    json={"warc": "!!!not-base64!!!",
                          "url": "http://a/", "at": "2021-01-01T00:00:00Z"})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_base64"


def test_size_limit_still_enforced():
    blob = b"x" * (MAX_DECODED_BYTES + 1)
    r = client.post("/v1/navigate",
                    json={"warc": base64.b64encode(blob).decode(),
                          "url": "http://a/", "at": "2021-01-01T00:00:00Z"})
    assert r.status_code == 422
    assert r.json()["error"] == "payload_too_large"


def test_broken_revisit_chain_still_422():
    # 即使断链 revisit 不参与任何选择，整包仍按统一 WARC 校验判 422
    rev = raw_record(
        "<urn:uuid:rev>", rec_type="revisit", uri="http://a/",
        block=b"", extra_headers=b"WARC-Refers-To: <urn:uuid:ghost>\r\n")
    r = nav(rev, "http://other/", "2021-06-01T00:00:00Z")
    assert r.status_code == 422
    assert r.json()["error"] == "target_not_found"


def test_gzip_warc_supported_in_navigate():
    def fill(w):
        w.write_record(_make_response(w, "<urn:uuid:1>", "http://a/",
                                      "2021-01-01T00:00:00Z", body=b"gz"))
    import gzip
    r = nav(gzip.compress(build_warc(fill)), "http://a/",
            "2021-06-01T00:00:00Z")
    assert r.status_code == 200
    assert base64.b64decode(r.json()["body_base64"]) == b"gz"


def test_empty_warc_navigate_is_404():
    r = nav(b"", "http://a/", "2021-06-01T00:00:00Z")
    assert r.status_code == 404
    d = r.json()
    assert d["error"] == "capture_not_found"
    assert d["url"] == "http://a/"
    assert d["path"] == []


def test_health_still_ok():
    assert client.get("/health").status_code == 200
