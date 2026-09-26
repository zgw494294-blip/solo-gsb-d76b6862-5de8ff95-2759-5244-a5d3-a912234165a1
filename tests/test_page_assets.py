"""/v1/page-assets 端到端测试：资源提取、base 解析、逐项可用性判定与错误场景。"""

from __future__ import annotations

import base64
import io

from fastapi.testclient import TestClient
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import BufferWARCWriter

from app.main import app

client = TestClient(app)


# ---------- WARC 构造辅助 ----------


def _make_response(w, rec_id, uri, date, status_code=200, reason="OK",
                   body=b"", location=None, with_http=True,
                   rec_type="response", refers_to=None):
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


def page_warc(page_html, fills, page_url="http://a/",
              page_date="2021-01-01T00:00:00Z", page_id="<urn:uuid:page>"):
    """页面记录 + 各资源记录的完整 WARC；fills 为 (rec_id, url, kw) 列表。"""
    def fill(w):
        w.write_record(_make_response(w, page_id, page_url, page_date,
                                      body=page_html))
        for rec_id, url, kw in fills:
            w.write_record(_make_response(w, rec_id, url, **kw))
    return build_warc(fill)


def pa(warc_bytes, url, at, **kw):
    return client.post(
        "/v1/page-assets",
        json={"warc": base64.b64encode(warc_bytes).decode(), "url": url,
              "at": at},
        **kw,
    )


def pa_b64(warc_b64, url, at, **kw):
    return client.post("/v1/page-assets",
                       json={"warc": warc_b64, "url": url, "at": at}, **kw)


AT = "2021-06-01T00:00:00Z"
D = "2021-01-01T00:00:00Z"


def assets_by_url(d):
    return {a["url"]: a for a in d["assets"]}


# ---------- 正常路径：提取与判定 ----------


def test_all_asset_kinds_extracted_and_usable():
    html = (b'<html><head><link rel="stylesheet" href="c.css"></head>'
            b'<body><img src="/p.png"><script src="http://a/s.js"></script>'
            b"</body></html>")
    warc = page_warc(html, [
        ("<urn:uuid:css>", "http://a/c.css", {"date": D, "body": b"css"}),
        ("<urn:uuid:png>", "http://a/p.png", {"date": D, "body": b"png"}),
        ("<urn:uuid:js>", "http://a/s.js", {"date": D, "body": b"js"}),
    ])
    r = pa(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["final_url"] == "http://a/"
    assert d["total"] == 3
    assert d["usable"] == 3
    # 按 URL 字典序
    assert [a["url"] for a in d["assets"]] == [
        "http://a/c.css", "http://a/p.png", "http://a/s.js"]
    by = assets_by_url(d)
    assert by["http://a/c.css"]["source"] == "link"
    assert by["http://a/p.png"]["source"] == "img"
    assert by["http://a/s.js"]["source"] == "script"
    for a in d["assets"]:
        assert a["ok"] is True
        assert a["error"] is None
        assert a["http_status"] == 200
        assert a["record_id"] is not None
    assert by["http://a/c.css"]["record_id"] == "<urn:uuid:css>"


def test_no_assets_returns_empty_lists():
    warc = page_warc(b"<html><body>plain text</body></html>", [])
    r = pa(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 0
    assert d["usable"] == 0
    assert d["assets"] == []


def test_case_insensitive_tags_attrs_and_rel():
    html = (b'<IMG SRC="i.png"><SCRIPT SRC="s.js"></SCRIPT>'
            b'<Link REL="StyleSheet" HREF="c.css">')
    warc = page_warc(html, [
        ("<urn:uuid:i>", "http://a/i.png", {"date": D}),
        ("<urn:uuid:s>", "http://a/s.js", {"date": D}),
        ("<urn:uuid:c>", "http://a/c.css", {"date": D}),
    ])
    r = pa(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 3
    assert d["usable"] == 3
    by = assets_by_url(d)
    assert by["http://a/i.png"]["source"] == "img"
    assert by["http://a/s.js"]["source"] == "script"
    assert by["http://a/c.css"]["source"] == "link"


def test_rel_token_list_and_non_stylesheet_link():
    # rel 为空白分隔的词表：含 stylesheet 即提取；icon 等非样式表不提取
    html = (b'<link rel="alternate stylesheet" href="alt.css">'
            b'<link rel="icon" href="favicon.ico">'
            b'<link rel="preload" href="p.js">')
    warc = page_warc(html, [
        ("<urn:uuid:alt>", "http://a/alt.css", {"date": D}),
    ])
    r = pa(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert [a["url"] for a in d["assets"]] == ["http://a/alt.css"]
    assert d["assets"][0]["source"] == "link"


def test_empty_and_missing_attributes_skipped():
    html = (b'<img src=""> <img src="  "> <img> '
            b'<script></script><script src=""></script>'
            b'<link rel="stylesheet"><link rel="stylesheet" href="">')
    warc = page_warc(html, [])
    r = pa(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    assert r.json()["assets"] == []


def test_fragment_stripped_and_dedup_keeps_first_source():
    html = (b'<img src="a.png#frag1"><script src="a.png#frag2"></script>'
            b'<img src="a.png">')
    warc = page_warc(html, [
        ("<urn:uuid:a>", "http://a/a.png", {"date": D}),
    ])
    r = pa(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 1
    item = d["assets"][0]
    assert item["url"] == "http://a/a.png"
    assert item["source"] == "img"  # 首次出现的来源标签
    assert item["ok"] is True


def test_non_http_resources_ignored():
    html = (b'<img src="data:image/png;base64,AAAA">'
            b'<script src="javascript:void(0)"></script>'
            b'<link rel="stylesheet" href="mailto:a@b">'
            b'<img src="ftp://a/f.png">'
            b'<img src="//cdn.example.com/x.png">')
    warc = page_warc(html, [
        ("<urn:uuid:x>", "http://cdn.example.com/x.png", {"date": D}),
    ])
    r = pa(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    # 协议相对地址按页面 scheme 解析为 http，是唯一被检查的项
    assert [a["url"] for a in d["assets"]] == ["http://cdn.example.com/x.png"]
    assert d["usable"] == 1


# ---------- base href ----------


def test_base_href_used_for_relative_urls():
    html = (b'<base href="http://cdn.example.com/static/">'
            b'<img src="i.png"><link rel="stylesheet" href="/root.css">')
    warc = page_warc(html, [
        ("<urn:uuid:i>", "http://cdn.example.com/static/i.png", {"date": D}),
        ("<urn:uuid:c>", "http://cdn.example.com/root.css", {"date": D}),
    ])
    r = pa(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert [a["url"] for a in d["assets"]] == [
        "http://cdn.example.com/root.css",
        "http://cdn.example.com/static/i.png",
    ]
    assert d["usable"] == 2


def test_relative_base_resolved_against_final_url():
    html = b'<base href="/static/"><img src="i.png">'
    warc = page_warc(html, [
        ("<urn:uuid:i>", "http://a/static/i.png", {"date": D}),
    ], page_url="http://a/dir/page")
    r = pa(warc, "http://a/dir/page", AT)
    assert r.status_code == 200, r.text
    assert [a["url"] for a in r.json()["assets"]] == [
        "http://a/static/i.png"]


def test_first_valid_base_wins_invalid_ones_skipped():
    # 第一个 base 非 HTTP(S)、第二个为空、第三个有效：用第三个
    html = (b'<base href="ftp://cdn/x/"><base href="">'
            b'<base href="http://cdn.example.com/s/">'
            b'<base href="http://other.example.com/ignored/">'
            b'<img src="i.png">')
    warc = page_warc(html, [
        ("<urn:uuid:i>", "http://cdn.example.com/s/i.png", {"date": D}),
    ])
    r = pa(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    assert [a["url"] for a in r.json()["assets"]] == [
        "http://cdn.example.com/s/i.png"]


def test_no_valid_base_falls_back_to_final_page_url():
    html = b'<base href="mailto:a@b"><img src="i.png">'
    warc = page_warc(html, [
        ("<urn:uuid:i>", "http://a/dir/i.png", {"date": D}),
    ], page_url="http://a/dir/page")
    r = pa(warc, "http://a/dir/page", AT)
    assert r.status_code == 200, r.text
    assert [a["url"] for a in r.json()["assets"]] == ["http://a/dir/i.png"]


def test_relative_urls_resolve_against_final_url_after_root_redirect():
    # 根页面 301 到 /home：相对资源按最终页 URL（而非起始 URL）解析
    html = b'<img src="i.png">'
    def fill(w):
        w.write_record(_make_response(
            w, "<urn:uuid:redir>", "http://a/start", D,
            status_code=301, reason="Moved", location="/home"))
        w.write_record(_make_response(
            w, "<urn:uuid:page>", "http://a/home", D, body=html))
        w.write_record(_make_response(
            w, "<urn:uuid:i>", "http://a/i.png", D, body=b"png"))
    r = pa(build_warc(fill), "http://a/start", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["final_url"] == "http://a/home"
    assert [a["url"] for a in d["assets"]] == ["http://a/i.png"]
    assert d["usable"] == 1


# ---------- 逐项判定：不使整页失败 ----------


def test_missing_capture_marked_per_item():
    html = b'<img src="have.png"><img src="gone.png">'
    warc = page_warc(html, [
        ("<urn:uuid:have>", "http://a/have.png", {"date": D}),
    ])
    r = pa(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 2
    assert d["usable"] == 1
    by = assets_by_url(d)
    assert by["http://a/have.png"]["ok"] is True
    gone = by["http://a/gone.png"]
    assert gone["ok"] is False
    assert gone["error"] == "capture_not_found"
    assert gone["record_id"] is None
    assert gone["http_status"] is None


def test_non_2xx_terminal_marked_per_item():
    html = b'<img src="nf.png"><img src="err.png"><img src="ok.png">'
    warc = page_warc(html, [
        ("<urn:uuid:nf>", "http://a/nf.png",
         {"date": D, "status_code": 404, "reason": "Not Found"}),
        ("<urn:uuid:err>", "http://a/err.png",
         {"date": D, "status_code": 500, "reason": "Server Error"}),
        ("<urn:uuid:ok>", "http://a/ok.png", {"date": D}),
    ])
    r = pa(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["usable"] == 1
    by = assets_by_url(d)
    nf = by["http://a/nf.png"]
    assert nf["ok"] is False
    assert nf["error"] == "status_not_2xx"
    assert nf["http_status"] == 404
    assert nf["record_id"] == "<urn:uuid:nf>"
    err = by["http://a/err.png"]
    assert err["error"] == "status_not_2xx"
    assert err["http_status"] == 500
    assert by["http://a/ok.png"]["ok"] is True


def test_asset_redirect_followed_to_2xx():
    html = b'<img src="old.png">'
    warc = page_warc(html, [
        ("<urn:uuid:r1>", "http://a/old.png",
         {"date": D, "status_code": 301, "reason": "Moved",
          "location": "/new.png"}),
        ("<urn:uuid:r2>", "http://a/new.png", {"date": D, "body": b"png"}),
    ])
    r = pa(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    item = r.json()["assets"][0]
    assert item["url"] == "http://a/old.png"
    assert item["ok"] is True
    assert item["error"] is None
    assert item["record_id"] == "<urn:uuid:r2>"  # 终点记录 ID
    assert item["http_status"] == 200


def test_asset_redirect_to_non_2xx_is_not_usable():
    html = b'<img src="r.png">'
    warc = page_warc(html, [
        ("<urn:uuid:r1>", "http://a/r.png",
         {"date": D, "status_code": 302, "location": "/t.png"}),
        ("<urn:uuid:r2>", "http://a/t.png",
         {"date": D, "status_code": 404, "reason": "Not Found"}),
    ])
    r = pa(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    item = r.json()["assets"][0]
    assert item["ok"] is False
    assert item["error"] == "status_not_2xx"
    assert item["record_id"] == "<urn:uuid:r2>"
    assert item["http_status"] == 404


def test_asset_redirect_errors_marked_per_item():
    html = (b'<img src="noloc.png"><img src="loop.png">'
            b'<img src="bad.png"><img src="ok.png">')
    warc = page_warc(html, [
        # 3xx 缺 Location
        ("<urn:uuid:noloc>", "http://a/noloc.png",
         {"date": D, "status_code": 302, "reason": "Found"}),
        # 重定向成环
        ("<urn:uuid:loop>", "http://a/loop.png",
         {"date": D, "status_code": 301, "location": "/loop.png"}),
        # Location 目标非 HTTP(S)
        ("<urn:uuid:bad>", "http://a/bad.png",
         {"date": D, "status_code": 301, "location": "mailto:a@b"}),
        ("<urn:uuid:ok>", "http://a/ok.png", {"date": D}),
    ])
    r = pa(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 4
    assert d["usable"] == 1
    by = assets_by_url(d)
    assert by["http://a/noloc.png"]["error"] == "missing_location"
    assert by["http://a/loop.png"]["error"] == "redirect_cycle"
    assert by["http://a/bad.png"]["error"] == "invalid_redirect_target"
    for url in ("noloc.png", "loop.png", "bad.png"):
        item = by[f"http://a/{url}"]
        assert item["ok"] is False
        assert item["record_id"] is None
        assert item["http_status"] is None
    assert by["http://a/ok.png"]["ok"] is True


def test_asset_selection_respects_point_in_time():
    # 资源在时点之后才有抓取 → 无抓取；时点前最新一条被选中
    html = b'<img src="t.png"><img src="u.png">'
    warc = page_warc(html, [
        ("<urn:uuid:old>", "http://a/t.png",
         {"date": "2020-01-01T00:00:00Z", "body": b"v1"}),
        ("<urn:uuid:new>", "http://a/t.png",
         {"date": "2021-03-01T00:00:00Z", "body": b"v2"}),
        ("<urn:uuid:future>", "http://a/u.png",
         {"date": "2022-01-01T00:00:00Z"}),
    ])
    r = pa(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    by = assets_by_url(r.json())
    assert by["http://a/t.png"]["record_id"] == "<urn:uuid:new>"
    assert by["http://a/t.png"]["ok"] is True
    assert by["http://a/u.png"]["error"] == "capture_not_found"


def test_revisit_asset_terminal_without_http_status_not_usable():
    # 终点 revisit 自身无 HTTP 头：状态为 null，非 2xx，不可用（但整页 200）
    html = b'<img src="rev.png">'
    def fill(w):
        w.write_record(_make_response(w, "<urn:uuid:page>", "http://a/", D,
                                      body=html))
        w.write_record(_make_response(w, "<urn:uuid:orig>",
                                      "http://a/orig.png", "2020-01-01T00:00:00Z",
                                      body=b"PNG"))
        w.write_record(_make_response(w, "<urn:uuid:rev>", "http://a/rev.png",
                                      D, rec_type="revisit", with_http=False,
                                      refers_to="<urn:uuid:orig>"))
    r = pa(build_warc(fill), "http://a/", AT)
    assert r.status_code == 200, r.text
    item = r.json()["assets"][0]
    assert item["ok"] is False
    assert item["error"] == "status_not_2xx"
    assert item["record_id"] == "<urn:uuid:rev>"
    assert item["http_status"] is None


def test_revisit_asset_with_own_2xx_status_usable():
    # revisit 自己带 200 HTTP 头：可用；正文还原不影响状态判定
    html = b'<img src="rev.png">'
    def fill(w):
        w.write_record(_make_response(w, "<urn:uuid:page>", "http://a/", D,
                                      body=html))
        w.write_record(_make_response(w, "<urn:uuid:orig>",
                                      "http://a/orig.png", "2020-01-01T00:00:00Z",
                                      body=b"PNG"))
        w.write_record(_make_response(w, "<urn:uuid:rev>", "http://a/rev.png",
                                      D, rec_type="revisit",
                                      refers_to="<urn:uuid:orig>"))
    r = pa(build_warc(fill), "http://a/", AT)
    assert r.status_code == 200, r.text
    item = r.json()["assets"][0]
    assert item["ok"] is True
    assert item["record_id"] == "<urn:uuid:rev>"
    assert item["http_status"] == 200


def test_page_body_from_revisit_endpoint_is_parsed():
    # 根页面自身是无 HTTP 头的 revisit：HTML 取自链终点 response 的正文
    html = b'<img src="i.png">'
    def fill(w):
        w.write_record(_make_response(w, "<urn:uuid:orig-page>",
                                      "http://a/orig-page", "2020-01-01T00:00:00Z",
                                      body=html))
        w.write_record(_make_response(w, "<urn:uuid:rev-page>", "http://a/",
                                      D, rec_type="revisit", with_http=False,
                                      refers_to="<urn:uuid:orig-page>"))
        w.write_record(_make_response(w, "<urn:uuid:i>", "http://a/i.png",
                                      D, body=b"png"))
    r = pa(build_warc(fill), "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 1
    assert d["usable"] == 1
    assert d["assets"][0]["url"] == "http://a/i.png"


def test_gzip_warc_supported():
    import gzip
    html = b'<img src="i.png">'
    warc = page_warc(html, [
        ("<urn:uuid:i>", "http://a/i.png", {"date": D}),
    ])
    r = pa(gzip.compress(warc), "http://a/", AT)
    assert r.status_code == 200, r.text
    assert r.json()["usable"] == 1


# ---------- 根页面 / 归档错误：沿用原接口响应 ----------


def test_root_page_no_capture_is_404():
    html = b'<img src="i.png">'
    warc = page_warc(html, [
        ("<urn:uuid:i>", "http://a/i.png", {"date": D}),
    ])
    r = pa(warc, "http://other/", AT)
    assert r.status_code == 404
    d = r.json()
    assert d["error"] == "capture_not_found"
    assert d["url"] == "http://other/"
    assert d["path"] == []


def test_root_page_redirect_error_is_422():
    def fill(w):
        w.write_record(_make_response(
            w, "<urn:uuid:1>", "http://a/", D,
            status_code=301, reason="Moved", location="/x"))
        w.write_record(_make_response(
            w, "<urn:uuid:2>", "http://a/x", D,
            status_code=302, reason="Found", location="/"))
    r = pa(build_warc(fill), "http://a/", AT)
    assert r.status_code == 422
    assert r.json()["error"] == "redirect_cycle"


def test_root_page_missing_location_is_422():
    rec = raw_record("<urn:uuid:1>", uri="http://a/",
                     block=http_block("HTTP/1.1 302 Found", body=b""))
    r = pa(rec, "http://a/", AT)
    assert r.status_code == 422
    assert r.json()["error"] == "missing_location"


def test_invalid_start_url_422():
    warc_b64 = base64.b64encode(b"").decode()
    for bad in ("ftp://a/", "not a url", "http://", ""):
        r = pa_b64(warc_b64, bad, AT)
        assert r.status_code == 422, bad
        assert r.json()["error"] == "invalid_start_url"


def test_invalid_at_422():
    r = pa_b64(base64.b64encode(b"").decode(), "http://a/", "not-a-time")
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_at"


def test_malformed_warc_422():
    r = pa_b64(base64.b64encode(b"garbage").decode(), "http://a/", AT)
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"


def test_invalid_base64_422():
    r = pa_b64("!!!not-base64!!!", "http://a/", AT)
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_base64"


def test_invalid_capture_date_in_warc_422():
    rec = raw_record("<urn:uuid:1>", uri="http://a/", date="not-a-date",
                     block=http_block(body=b"<img src='i.png'>"))
    r = pa(rec, "http://a/", AT)
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_capture_date"


def test_broken_revisit_chain_still_422():
    rev = raw_record(
        "<urn:uuid:rev>", rec_type="revisit", uri="http://a/",
        block=b"", extra_headers=b"WARC-Refers-To: <urn:uuid:ghost>\r\n")
    r = pa(rev, "http://a/", AT)
    assert r.status_code == 422
    assert r.json()["error"] == "target_not_found"


def test_missing_json_fields_422():
    r = client.post("/v1/page-assets", json={"warc": ""})
    assert r.status_code == 422


def test_empty_warc_root_is_404():
    r = pa(b"", "http://a/", AT)
    assert r.status_code == 404
    assert r.json()["error"] == "capture_not_found"
