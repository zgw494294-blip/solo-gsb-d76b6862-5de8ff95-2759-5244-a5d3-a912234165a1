"""POST /v1/snapshot-diff 端到端测试：时点快照、四类对比与错误场景。"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io

import pytest
from fastapi.testclient import TestClient
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import BufferWARCWriter

from app.main import app
from app.replay import MAX_DECODED_BYTES

client = TestClient(app)


# ---------- WARC 构造辅助 ----------


def _make_response(w, rec_id, uri, date, status_code=200, reason="OK",
                   body=b"", with_http=True, rec_type="response",
                   refers_to=None):
    if with_http:
        headers = [("Content-Length", str(len(body)))]
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


def diff(old_bytes, new_bytes, at, **kw):
    return client.post(
        "/v1/snapshot-diff",
        json={
            "old_warc": base64.b64encode(old_bytes).decode(),
            "new_warc": base64.b64encode(new_bytes).decode(),
            "at": at,
        },
        **kw,
    )


def diff_b64(old_b64, new_b64, at, **kw):
    return client.post(
        "/v1/snapshot-diff",
        json={"old_warc": old_b64, "new_warc": new_b64, "at": at},
        **kw,
    )


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------- 正常路径：四类对比 ----------


def _four_category_warcs():
    def fill_old(w):
        w.write_record(_make_response(w, "<urn:uuid:o1>", "http://a/",
                                      "2020-01-01T00:00:00Z", body=b"same"))
        w.write_record(_make_response(w, "<urn:uuid:o2>", "http://b/",
                                      "2020-01-01T00:00:00Z", body=b"old-body"))
        w.write_record(_make_response(w, "<urn:uuid:o3>", "http://c/",
                                      "2020-01-01T00:00:00Z", body=b"gone"))
        w.write_record(_make_response(w, "<urn:uuid:o4>", "http://d/",
                                      "2020-01-01T00:00:00Z", body=b"same-d"))

    def fill_new(w):
        # 记录 ID 与抓取时间都不同，但正文与状态一致 → 未变化
        w.write_record(_make_response(w, "<urn:uuid:n1>", "http://a/",
                                      "2021-01-01T00:00:00Z", body=b"same"))
        w.write_record(_make_response(w, "<urn:uuid:n2>", "http://b/",
                                      "2021-01-01T00:00:00Z", body=b"new-body"))
        # 正文相同、自身状态不同 → 内容变化
        w.write_record(_make_response(w, "<urn:uuid:n4>", "http://d/",
                                      "2021-01-01T00:00:00Z",
                                      status_code=404, reason="Not Found",
                                      body=b"same-d"))
        w.write_record(_make_response(w, "<urn:uuid:n5>", "http://e/",
                                      "2021-01-01T00:00:00Z", body=b"fresh"))
    return build_warc(fill_old), build_warc(fill_new)


def test_four_categories_summary_and_items():
    old_warc, new_warc = _four_category_warcs()
    r = diff(old_warc, new_warc, "2022-01-01T00:00:00Z")
    assert r.status_code == 200, r.text
    d = r.json()

    assert d["summary"] == {
        "only_old": 1, "only_new": 1, "changed": 2, "unchanged": 1,
    }

    assert [i["url"] for i in d["only_old"]] == ["http://c/"]
    item = d["only_old"][0]
    assert item["old"] == {
        "record_id": "<urn:uuid:o3>",
        "http_status": 200,
        "body_sha256": sha256(b"gone"),
    }
    assert item["new"] is None

    assert [i["url"] for i in d["only_new"]] == ["http://e/"]
    item = d["only_new"][0]
    assert item["old"] is None
    assert item["new"] == {
        "record_id": "<urn:uuid:n5>",
        "http_status": 200,
        "body_sha256": sha256(b"fresh"),
    }

    # changed 按 URL 字典序；正文变化与状态变化各一条
    assert [i["url"] for i in d["changed"]] == ["http://b/", "http://d/"]
    body_changed, status_changed = d["changed"]
    assert body_changed["old"]["record_id"] == "<urn:uuid:o2>"
    assert body_changed["new"]["record_id"] == "<urn:uuid:n2>"
    assert body_changed["old"]["body_sha256"] == sha256(b"old-body")
    assert body_changed["new"]["body_sha256"] == sha256(b"new-body")
    assert status_changed["old"]["http_status"] == 200
    assert status_changed["new"]["http_status"] == 404
    assert status_changed["old"]["body_sha256"] \
        == status_changed["new"]["body_sha256"] == sha256(b"same-d")

    assert [i["url"] for i in d["unchanged"]] == ["http://a/"]
    item = d["unchanged"][0]
    assert item["old"]["record_id"] == "<urn:uuid:o1>"
    assert item["new"]["record_id"] == "<urn:uuid:n1>"
    assert item["old"]["body_sha256"] == item["new"]["body_sha256"] \
        == sha256(b"same")


def test_urls_sorted_lexicographically_within_each_category():
    def fill_old(w):
        for i, url in enumerate(["http://b/2", "http://a/", "http://b/10"]):
            w.write_record(_make_response(w, f"<urn:uuid:o{i}>", url,
                                          "2020-01-01T00:00:00Z", body=b"x"))
    r = diff(build_warc(fill_old), b"", "2021-01-01T00:00:00Z")
    assert r.status_code == 200, r.text
    d = r.json()
    # 字典序（码位）："http://a/" < "http://b/10" < "http://b/2"
    assert [i["url"] for i in d["only_old"]] == [
        "http://a/", "http://b/10", "http://b/2"]
    assert d["summary"]["only_old"] == 3


def test_empty_archives_all_counts_zero():
    r = diff(b"", b"", "2021-01-01T00:00:00Z")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["summary"] == {
        "only_old": 0, "only_new": 0, "changed": 0, "unchanged": 0}
    assert d["only_old"] == d["only_new"] == []
    assert d["changed"] == d["unchanged"] == []


def test_one_side_empty_everything_only_on_other_side():
    def fill(w):
        w.write_record(_make_response(w, "<urn:uuid:1>", "http://a/",
                                      "2020-01-01T00:00:00Z", body=b"a"))
        w.write_record(_make_response(w, "<urn:uuid:2>", "http://b/",
                                      "2020-01-01T00:00:00Z", body=b"b"))
    warc = build_warc(fill)
    r = diff(warc, b"", "2021-01-01T00:00:00Z")
    assert [i["url"] for i in r.json()["only_new"]] == []
    assert [i["url"] for i in r.json()["only_old"]] == ["http://a/", "http://b/"]
    r = diff(b"", warc, "2021-01-01T00:00:00Z")
    assert [i["url"] for i in r.json()["only_old"]] == []
    assert [i["url"] for i in r.json()["only_new"]] == ["http://a/", "http://b/"]


# ---------- 快照选取规则（与 navigate 一致） ----------


def test_snapshot_respects_point_in_time():
    def fill_old(w):
        w.write_record(_make_response(w, "<urn:uuid:o1>", "http://a/",
                                      "2020-01-01T00:00:00Z", body=b"v1"))
        w.write_record(_make_response(w, "<urn:uuid:o2>", "http://a/",
                                      "2022-01-01T00:00:00Z", body=b"v2"))

    def fill_new(w):
        # 新版对 http://a/ 的唯一抓取在时点之后 → 该时点不可见
        w.write_record(_make_response(w, "<urn:uuid:n1>", "http://a/",
                                      "2022-06-01T00:00:00Z", body=b"v3"))
    old_warc, new_warc = build_warc(fill_old), build_warc(fill_new)

    # 时点取 2021：旧版选中 o1，新版不可见 → 仅旧版
    r = diff(old_warc, new_warc, "2021-01-01T00:00:00Z")
    assert r.status_code == 200, r.text
    d = r.json()
    assert [i["url"] for i in d["only_old"]] == ["http://a/"]
    assert d["only_old"][0]["old"]["record_id"] == "<urn:uuid:o1>"

    # 时点取 2023：旧版选中 o2（不晚于时点的最新），新版选中 n1 → 变化
    r = diff(old_warc, new_warc, "2023-01-01T00:00:00Z")
    d = r.json()
    assert [i["url"] for i in d["changed"]] == ["http://a/"]
    item = d["changed"][0]
    assert item["old"]["record_id"] == "<urn:uuid:o2>"
    assert item["new"]["record_id"] == "<urn:uuid:n1>"


def test_capture_exactly_at_point_in_time_is_visible():
    def fill(w):
        w.write_record(_make_response(w, "<urn:uuid:1>", "http://a/",
                                      "2021-01-01T00:00:00Z", body=b"exact"))
    warc = build_warc(fill)
    r = diff(warc, b"", "2021-01-01T00:00:00Z")
    assert r.status_code == 200
    assert r.json()["only_old"][0]["old"]["record_id"] == "<urn:uuid:1>"


def test_same_timestamp_uses_later_archive_order_per_archive():
    def fill_old(w):
        w.write_record(_make_response(w, "<urn:uuid:earlier>", "http://a/",
                                      "2021-01-01T00:00:00Z", body=b"first"))
        w.write_record(_make_response(w, "<urn:uuid:later>", "http://a/",
                                      "2021-01-01T00:00:00Z", body=b"second"))
    r = diff(build_warc(fill_old), b"", "2021-06-01T00:00:00Z")
    assert r.status_code == 200, r.text
    item = r.json()["only_old"][0]
    assert item["old"]["record_id"] == "<urn:uuid:later>"
    assert item["old"]["body_sha256"] == sha256(b"second")


def test_offset_timezone_normalized_to_utc():
    def fill_old(w):
        w.write_record(_make_response(w, "<urn:uuid:o>", "http://a/",
                                      "2021-01-01T02:00:00Z", body=b"z"))

    def fill_new(w):
        w.write_record(_make_response(w, "<urn:uuid:n>", "http://a/",
                                      "2021-01-01T05:00:00Z", body=b"later"))
    # 04:00+02:00 == 02:00Z：旧版恰好可见，新版还在未来
    r = diff(build_warc(fill_old), build_warc(fill_new),
             "2021-01-01T04:00:00+02:00")
    assert r.status_code == 200, r.text
    d = r.json()
    assert [i["url"] for i in d["only_old"]] == ["http://a/"]
    assert d["only_old"][0]["old"]["record_id"] == "<urn:uuid:o>"


def test_url_match_is_exact_no_normalization():
    def fill_old(w):
        w.write_record(_make_response(w, "<urn:uuid:o>", "http://a/",
                                      "2020-01-01T00:00:00Z", body=b"x"))

    def fill_new(w):
        w.write_record(_make_response(w, "<urn:uuid:n>", "http://A/",
                                      "2020-01-01T00:00:00Z", body=b"x"))
    r = diff(build_warc(fill_old), build_warc(fill_new), "2021-01-01T00:00:00Z")
    assert r.status_code == 200, r.text
    d = r.json()
    # 大小写不同的 URL 是不同键，不做规范化合并
    assert [i["url"] for i in d["only_old"]] == ["http://a/"]
    assert [i["url"] for i in d["only_new"]] == ["http://A/"]


# ---------- 内容变化判定 ----------


def test_record_id_date_and_body_source_changes_are_not_content_changes():
    # 旧版 http://a/ 为 response；新版同 URL 为 revisit（自己的 200 头），
    # 正文引自另一条记录：ID、抓取时间、正文来源全不同，但还原正文与
    # 自身状态一致 → 未变化。两侧都带同一份源记录（同 ID 跨归档合法）。
    def src(w, rec_id="<urn:uuid:src>"):
        return _make_response(w, rec_id, "http://src/", "2020-01-01T00:00:00Z",
                              body=b"BODY")

    def fill_old(w):
        w.write_record(src(w))
        w.write_record(_make_response(w, "<urn:uuid:o>", "http://a/",
                                      "2020-06-01T00:00:00Z", body=b"BODY"))

    def fill_new(w):
        w.write_record(src(w))
        w.write_record(_make_response(
            w, "<urn:uuid:n>", "http://a/", "2021-06-01T00:00:00Z",
            rec_type="revisit", refers_to="<urn:uuid:src>"))
    r = diff(build_warc(fill_old), build_warc(fill_new), "2022-01-01T00:00:00Z")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["summary"] == {
        "only_old": 0, "only_new": 0, "changed": 0, "unchanged": 2}
    assert [i["url"] for i in d["unchanged"]] == ["http://a/", "http://src/"]
    item = d["unchanged"][0]
    assert item["old"]["record_id"] == "<urn:uuid:o>"
    assert item["new"]["record_id"] == "<urn:uuid:n>"
    assert item["old"]["body_sha256"] == item["new"]["body_sha256"] \
        == sha256(b"BODY")


def test_revisit_without_http_headers_has_null_status():
    def fill_old(w):
        w.write_record(_make_response(w, "<urn:uuid:src>", "http://src/",
                                      "2020-01-01T00:00:00Z", body=b"BODY"))
        # 无 HTTP 头的 revisit：状态为 null
        w.write_record(_make_response(
            w, "<urn:uuid:rev>", "http://a/", "2021-01-01T00:00:00Z",
            rec_type="revisit", with_http=False, refers_to="<urn:uuid:src>"))

    def fill_new(w):
        w.write_record(_make_response(w, "<urn:uuid:src>", "http://src/",
                                      "2020-01-01T00:00:00Z", body=b"BODY"))
        w.write_record(_make_response(w, "<urn:uuid:resp>", "http://a/",
                                      "2021-01-01T00:00:00Z", body=b"BODY"))
    r = diff(build_warc(fill_old), build_warc(fill_new), "2022-01-01T00:00:00Z")
    assert r.status_code == 200, r.text
    d = r.json()
    # 正文相同但 null ≠ 200 → 内容变化
    assert [i["url"] for i in d["changed"]] == ["http://a/"]
    item = d["changed"][0]
    assert item["old"]["http_status"] is None
    assert item["old"]["record_id"] == "<urn:uuid:rev>"
    assert item["old"]["body_sha256"] == sha256(b"BODY")
    assert item["new"]["http_status"] == 200
    assert [i["url"] for i in d["unchanged"]] == ["http://src/"]


def test_two_headerless_revisits_same_body_unchanged():
    def fill(w, rev_id):
        def inner(w_):
            w_.write_record(_make_response(
                w_, "<urn:uuid:src>", "http://src/", "2020-01-01T00:00:00Z",
                body=b"BODY"))
            w_.write_record(_make_response(
                w_, rev_id, "http://a/", "2021-01-01T00:00:00Z",
                rec_type="revisit", with_http=False, refers_to="<urn:uuid:src>"))
        return inner
    r = diff(build_warc(fill(None, "<urn:uuid:rev-o>")),
             build_warc(fill(None, "<urn:uuid:rev-n>")), "2022-01-01T00:00:00Z")
    assert r.status_code == 200, r.text
    d = r.json()
    assert [i["url"] for i in d["unchanged"]] == ["http://a/", "http://src/"]
    assert d["unchanged"][0]["old"]["http_status"] is None
    assert d["unchanged"][0]["new"]["http_status"] is None


def test_multilevel_revisit_chain_body_compared():
    # 新版 http://a/ 的 revisit 经多级链还原到终点正文；与旧版不同 → 变化
    def fill_old(w):
        w.write_record(_make_response(w, "<urn:uuid:o>", "http://a/",
                                      "2020-01-01T00:00:00Z", body=b"v1"))

    def fill_new(w):
        w.write_record(_make_response(w, "<urn:uuid:base>", "http://base/",
                                      "2020-01-01T00:00:00Z", body=b"v2"))
        w.write_record(_make_response(
            w, "<urn:uuid:mid>", "http://mid/", "2021-01-01T00:00:00Z",
            rec_type="revisit", with_http=False, refers_to="<urn:uuid:base>"))
        w.write_record(_make_response(
            w, "<urn:uuid:top>", "http://a/", "2021-06-01T00:00:00Z",
            rec_type="revisit", with_http=False, refers_to="<urn:uuid:mid>"))
    r = diff(build_warc(fill_old), build_warc(fill_new), "2022-01-01T00:00:00Z")
    assert r.status_code == 200, r.text
    d = r.json()
    assert [i["url"] for i in d["changed"]] == ["http://a/"]
    item = d["changed"][0]
    assert item["new"]["record_id"] == "<urn:uuid:top>"
    assert item["new"]["body_sha256"] == sha256(b"v2")


# ---------- 缺 URL / 日期记录的排除 ----------


def test_records_without_url_or_date_excluded_from_snapshot():
    no_url = raw_record("<urn:uuid:nouri>", uri=None,
                        block=http_block(body=b"orphan"))
    no_date_block = http_block(body=b"")
    no_date = (
        b"WARC/1.1\r\nWARC-Type: response\r\nWARC-Record-ID: <urn:uuid:nodate>\r\n"
        b"WARC-Target-URI: http://a/\r\n"
        b"Content-Length: " + str(len(no_date_block)).encode() + b"\r\n\r\n"
        + no_date_block + b"\r\n\r\n"
    )
    good = raw_record("<urn:uuid:good>", uri="http://a/",
                      block=http_block(body=b"good"))
    # 缺 URL / 日期的记录不入快照，也不连累其余记录；两侧归档都容忍
    r = diff(no_url + no_date + good, good, "2021-01-01T00:00:00Z")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["summary"] == {
        "only_old": 0, "only_new": 0, "changed": 0, "unchanged": 1}
    assert [i["url"] for i in d["unchanged"]] == ["http://a/"]


def test_only_url_less_captures_means_url_absent_not_only_old():
    no_url = raw_record("<urn:uuid:nouri>", uri=None,
                        block=http_block(body=b"orphan"))
    r = diff(no_url, b"", "2021-01-01T00:00:00Z")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["summary"] == {
        "only_old": 0, "only_new": 0, "changed": 0, "unchanged": 0}


# ---------- 错误场景 ----------


@pytest.mark.parametrize("side", ["old", "new"])
def test_invalid_capture_date_422(side):
    bad = raw_record("<urn:uuid:bad>", uri="http://a/", date="not-a-date",
                     block=http_block(body=b"x"))
    good = raw_record("<urn:uuid:good>", uri="http://a/",
                      block=http_block(body=b"good"))
    old, new = (bad, good) if side == "old" else (good, bad)
    r = diff(old, new, "2021-01-01T00:00:00Z")
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "invalid_capture_date"
    assert d["record_id"] == "<urn:uuid:bad>"
    assert d["warc_date"] == "not-a-date"


@pytest.mark.parametrize("bad_at", [
    "2021-13-01T00:00:00Z",
    "2021/01/01 00:00:00",
    "2021-01-01",
    "2021-01-01T00:00:00",
    "not-a-time",
])
def test_invalid_at_422(bad_at):
    r = diff(b"", b"", bad_at)
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_at"


def test_missing_json_fields_422():
    assert client.post("/v1/snapshot-diff", json={}).status_code == 422
    assert client.post("/v1/snapshot-diff",
                       json={"old_warc": "", "new_warc": ""}).status_code == 422
    assert client.post("/v1/snapshot-diff",
                       json={"old_warc": "", "at": "2021-01-01T00:00:00Z"}
                       ).status_code == 422


def test_same_record_id_in_both_archives_is_fine():
    # 重复记录 ID 仅在本份归档内判错；跨归档同名 ID 互不影响
    rec = raw_record("<urn:uuid:same>", uri="http://a/",
                     block=http_block(body=b"x"))
    r = diff(rec, rec, "2021-01-01T00:00:00Z")
    assert r.status_code == 200, r.text
    d = r.json()
    assert [i["url"] for i in d["unchanged"]] == ["http://a/"]
    assert d["unchanged"][0]["old"]["record_id"] == "<urn:uuid:same>"
    assert d["unchanged"][0]["new"]["record_id"] == "<urn:uuid:same>"


@pytest.mark.parametrize("side", ["old", "new"])
def test_duplicate_record_id_within_one_archive_422(side):
    dup1 = raw_record("<urn:uuid:dup>", uri="http://a/",
                      block=http_block(body=b"x"))
    dup2 = raw_record("<urn:uuid:dup>", uri="http://b/",
                      block=http_block(body=b"y"))
    good = raw_record("<urn:uuid:good>", uri="http://a/",
                      block=http_block(body=b"good"))
    old, new = (dup1 + dup2, good) if side == "old" else (good, dup1 + dup2)
    r = diff(old, new, "2021-01-01T00:00:00Z")
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "duplicate_record_id"
    assert d["record_id"] == "<urn:uuid:dup>"


@pytest.mark.parametrize("side", ["old", "new"])
def test_each_archive_validated_independently(side):
    good = raw_record("<urn:uuid:good>", uri="http://a/",
                      block=http_block(body=b"good"))
    rev_broken = raw_record(
        "<urn:uuid:rev>", rec_type="revisit", uri="http://a/",
        block=b"", extra_headers=b"WARC-Refers-To: <urn:uuid:ghost>\r\n")
    old, new = (good, rev_broken) if side == "new" else (rev_broken, good)
    r = diff(old, new, "2021-01-01T00:00:00Z")
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "target_not_found"
    assert d["missing_id"] == "<urn:uuid:ghost>"


def test_malformed_old_archive_422():
    good = raw_record("<urn:uuid:good>", uri="http://a/",
                      block=http_block(body=b"good"))
    r = diff(b"garbage", good, "2021-01-01T00:00:00Z")
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"


def test_invalid_base64_422():
    r = diff_b64("!!!not-base64!!!", "", "2021-01-01T00:00:00Z")
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_base64"
    r = diff_b64("", "!!!not-base64!!!", "2021-01-01T00:00:00Z")
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_base64"


@pytest.mark.parametrize("side", ["old", "new"])
def test_size_limit_enforced_per_archive(side):
    blob = b"x" * (MAX_DECODED_BYTES + 1)
    good = raw_record("<urn:uuid:good>", uri="http://a/",
                      block=http_block(body=b"good"))
    old, new = (blob, good) if side == "old" else (good, blob)
    r = diff(old, new, "2021-01-01T00:00:00Z")
    assert r.status_code == 422
    assert r.json()["error"] == "payload_too_large"


def test_gzip_archives_supported():
    def fill_old(w):
        w.write_record(_make_response(w, "<urn:uuid:o>", "http://a/",
                                      "2020-01-01T00:00:00Z", body=b"gz"))

    def fill_new(w):
        w.write_record(_make_response(w, "<urn:uuid:n>", "http://a/",
                                      "2020-01-01T00:00:00Z", body=b"gz"))
    # 一侧整条 gzip，另一侧多 member 拼接
    old_gz = gzip.compress(build_warc(fill_old))
    new_raw = build_warc(fill_new)
    half = len(new_raw) // 2
    new_gz = gzip.compress(new_raw[:half]) + gzip.compress(new_raw[half:])
    r = diff(old_gz, new_gz, "2021-01-01T00:00:00Z")
    assert r.status_code == 200, r.text
    assert [i["url"] for i in r.json()["unchanged"]] == ["http://a/"]


def test_health_and_other_endpoints_still_ok():
    assert client.get("/health").status_code == 200
    r = client.post("/v1/replay", json={"warc": ""})
    assert r.status_code == 200
    assert r.json() == {"count": 0, "records": []}
