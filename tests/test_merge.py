"""POST /v1/merge 端到端测试：双包合并、跨包 ID 冲突确定性改名、
缺 Content-Length 末记录的拼接处理与回放等价。"""

from __future__ import annotations

import base64
import gzip
import io

import pytest
from fastapi.testclient import TestClient
from warcio.archiveiterator import ArchiveIterator

from app.main import app
from app.replay import MAX_DECODED_BYTES

client = TestClient(app)


# ---------- WARC 构造辅助（与 test_extract.py 同风格） ----------


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


def no_cl_record(rec_id="<urn:uuid:a>", block=None, uri="http://e.com/"):
    """缺 Content-Length 的末记录：记录块延伸至归档 EOF。"""
    if block is None:
        block = http_block(body=b"hello") + b"\r\n\r\n"
    return (
        f"WARC/1.1\r\nWARC-Type: response\r\n"
        f"WARC-Record-ID: {rec_id}\r\n"
        f"WARC-Date: 2020-01-01T00:00:00Z\r\n"
        f"WARC-Target-URI: {uri}\r\n".encode()
        + b"\r\n" + block
    )


def post_merge(left, right, **kw):
    return client.post(
        "/v1/merge",
        json={"left_warc": base64.b64encode(left).decode(),
              "right_warc": base64.b64encode(right).decode()},
        **kw,
    )


def merged_bytes(response):
    assert response.status_code == 200, response.text
    return base64.b64decode(response.json()["merged_warc"])


def replay_records(warc_bytes):
    r = client.post("/v1/replay",
                    json={"warc": base64.b64encode(warc_bytes).decode()})
    assert r.status_code == 200, r.text
    return r.json()["records"]


def bodies(records):
    return [base64.b64decode(rec["body_base64"]) for rec in records]


# ---------- 正常路径：无冲突合并 ----------


def test_merge_basic_left_then_right_order():
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))
    b = raw_record("<urn:uuid:b>", block=http_block(body=b"bb"),
                   uri="http://b.example/")
    c = raw_record("<urn:uuid:c>", block=http_block(
        "HTTP/1.1 404 Not Found", body=b"cc"), uri="http://c.example/")
    r = post_merge(a + b, c)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 3
    assert data["renamed"] == []
    records = replay_records(merged_bytes(r))
    assert [rec["record_id"] for rec in records] == [
        "<urn:uuid:a>", "<urn:uuid:b>", "<urn:uuid:c>"]
    assert [rec["order"] for rec in records] == [0, 1, 2]
    assert bodies(records) == [b"aa", b"bb", b"cc"]
    assert records[2]["http_status"] == 404


def test_merge_no_conflict_preserves_record_bytes_verbatim():
    # 无冲突且各记录都带 Content-Length：合并字节 == 两包拼接去掉末尾分隔符
    a = raw_record("<urn:uuid:a>", block=http_block(
        "HTTP/1.1 200 OK", [("X-Custom", "v1")], body=b"\x00\x01aa\xff"))
    b = raw_record("<urn:uuid:b>", block=http_block(body=b"bb"),
                   uri="http://b.example/", date="2021-05-05T10:00:00Z",
                   extra_headers=b"WARC-Block-Digest: sha1:ABC\r\n")
    left, right = a, b
    r = post_merge(left, right)
    assert r.status_code == 200, r.text
    assert merged_bytes(r) == (left + right)[: -len(b"\r\n\r\n")]


def test_merge_content_lengths_match_written_blocks():
    # 逐条核对合并包中每条记录声明的 Content-Length 与实际块消费量
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"hello"))
    rev = raw_record("<urn:uuid:rev>", rec_type="revisit", block=b"",
                     date="2021-01-01T00:00:00Z",
                     extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    c = raw_record("<urn:uuid:c>", block=http_block(body=b"cc"),
                   uri="http://c.example/")
    r = post_merge(a + rev, c)
    seen = []
    for rec in ArchiveIterator(io.BytesIO(merged_bytes(r))):
        declared = int(rec.rec_headers.get_header("Content-Length"))
        rec.raw_stream.read()
        # tell() 含 warcio 解析时预读的 HTTP 头，等于整块消费量
        assert rec.raw_stream.tell() == declared
        seen.append(rec.rec_headers.get_header("WARC-Record-ID"))
    assert seen == ["<urn:uuid:a>", "<urn:uuid:rev>", "<urn:uuid:c>"]


def test_merge_skips_non_target_record_types():
    info = raw_record("<urn:uuid:info>", rec_type="warcinfo",
                      block=b"info: x\r\n")
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))
    r = post_merge(info + a, b"")
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1
    records = replay_records(merged_bytes(r))
    assert [rec["record_id"] for rec in records] == ["<urn:uuid:a>"]


def test_merge_empty_archives():
    r = post_merge(b"", b"")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 0
    assert data["merged_warc"] == ""
    assert data["renamed"] == []
    assert replay_records(base64.b64decode(data["merged_warc"])) == []


def test_merge_one_side_empty():
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))
    for left, right in ((b"", a), (a, b"")):
        r = post_merge(left, right)
        assert r.status_code == 200, r.text
        assert r.json()["count"] == 1
        records = replay_records(merged_bytes(r))
        assert [rec["record_id"] for rec in records] == ["<urn:uuid:a>"]
        assert bodies(records) == [b"aa"]


def test_merge_gzip_inputs():
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"gz-l"))
    b = raw_record("<urn:uuid:b>", block=http_block(body=b"gz-r"),
                   uri="http://b.example/")
    r = post_merge(gzip.compress(a), gzip.compress(b))
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2
    assert bodies(replay_records(merged_bytes(r))) == [b"gz-l", b"gz-r"]


# ---------- 跨包 ID 冲突：确定性改名与引用改写 ----------


def test_merge_id_conflict_renames_right_only():
    left = raw_record("<urn:uuid:a>", block=http_block(body=b"LEFT"))
    right = raw_record("<urn:uuid:a>", block=http_block(body=b"RIGHT"))
    r = post_merge(left, right)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 2
    renamed = data["renamed"]
    assert len(renamed) == 1
    assert renamed[0]["old_id"] == "<urn:uuid:a>"
    new_id = renamed[0]["new_id"]
    assert new_id.startswith("<urn:uuid:") and new_id != "<urn:uuid:a>"

    records = replay_records(merged_bytes(r))
    assert [rec["record_id"] for rec in records] == ["<urn:uuid:a>", new_id]
    assert bodies(records) == [b"LEFT", b"RIGHT"]
    assert records[0]["body_source_id"] == "<urn:uuid:a>"
    assert records[1]["body_source_id"] == new_id


def test_merge_deterministic_across_requests():
    left = raw_record("<urn:uuid:a>", block=http_block(body=b"LEFT"))
    right = raw_record("<urn:uuid:a>", block=http_block(body=b"RIGHT"))
    r1, r2 = post_merge(left, right), post_merge(left, right)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()


def test_merge_conflict_detected_after_normalization():
    # 左包尖括号形式、右包裸 URN：规范化后相同即冲突
    left = raw_record("<urn:uuid:a>", block=http_block(body=b"LEFT"))
    right = raw_record("urn:uuid:a", block=http_block(body=b"RIGHT"))
    r = post_merge(left, right)
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["renamed"]) == 1
    assert data["renamed"][0]["old_id"] == "urn:uuid:a"
    records = replay_records(merged_bytes(r))
    assert records[0]["record_id"] == "<urn:uuid:a>"
    assert records[1]["record_id"] == data["renamed"][0]["new_id"]
    assert bodies(records) == [b"LEFT", b"RIGHT"]


def test_merge_multiple_conflicts_get_distinct_new_ids():
    left = (raw_record("<urn:uuid:a>", block=http_block(body=b"L1"))
            + raw_record("<urn:uuid:b>", block=http_block(body=b"L2"),
                         uri="http://b.example/"))
    right = (raw_record("<urn:uuid:a>", block=http_block(body=b"R1"))
             + raw_record("<urn:uuid:b>", block=http_block(body=b"R2"),
                          uri="http://b.example/")
             + raw_record("<urn:uuid:c>", block=http_block(body=b"R3"),
                          uri="http://c.example/"))
    r = post_merge(left, right)
    assert r.status_code == 200, r.text
    data = r.json()
    renamed = data["renamed"]
    # 按右包原序给出对照
    assert [x["old_id"] for x in renamed] == ["<urn:uuid:a>", "<urn:uuid:b>"]
    new_ids = [x["new_id"] for x in renamed]
    assert len(set(new_ids)) == 2
    assert "<urn:uuid:a>" not in new_ids and "<urn:uuid:b>" not in new_ids

    records = replay_records(merged_bytes(r))
    assert [rec["record_id"] for rec in records] == [
        "<urn:uuid:a>", "<urn:uuid:b>", new_ids[0], new_ids[1],
        "<urn:uuid:c>"]
    assert bodies(records) == [b"L1", b"L2", b"R1", b"R2", b"R3"]


def test_merge_conflict_rewrites_right_refers_to():
    # 右包 revisit 引用右包冲突记录：WARC-Refers-To 同步改写为新 ID
    left = raw_record("<urn:uuid:a>", block=http_block(body=b"LEFT"))
    right_resp = raw_record("<urn:uuid:a>", block=http_block(body=b"RIGHT"))
    right_rev = raw_record(
        "<urn:uuid:v>", rec_type="revisit", block=b"",
        uri="http://v.example/", date="2021-01-01T00:00:00Z",
        extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    r = post_merge(left, right_resp + right_rev)
    assert r.status_code == 200, r.text
    data = r.json()
    new_id = data["renamed"][0]["new_id"]

    merged = merged_bytes(r)
    # 右包 response 的 WARC-Record-ID 与右包 revisit 的 WARC-Refers-To 被改写
    assert f"WARC-Record-ID: {new_id}\r\n".encode() in merged
    assert f"WARC-Refers-To: {new_id}\r\n".encode() in merged
    assert b"WARC-Refers-To: <urn:uuid:a>\r\n" not in merged
    # 左包记录保持原样
    assert b"WARC-Record-ID: <urn:uuid:a>\r\n" in merged

    records = replay_records(merged)
    assert [rec["record_id"] for rec in records] == [
        "<urn:uuid:a>", new_id, "<urn:uuid:v>"]
    # 右包 revisit 的正文来自改名后的右包 response，正文来源 ID 随映射替换
    assert bodies(records) == [b"LEFT", b"RIGHT", b"RIGHT"]
    assert records[2]["body_source_id"] == new_id


def test_merge_refers_to_rewrite_regardless_of_bracket_style():
    # 右包记录裸 URN、右包 revisit 尖括号引用：规范化后同一 ID，都要改写
    left = raw_record("<urn:uuid:a>", block=http_block(body=b"LEFT"))
    right_resp = raw_record("urn:uuid:a", block=http_block(body=b"RIGHT"))
    right_rev = raw_record(
        "<urn:uuid:v>", rec_type="revisit", block=b"",
        uri="http://v.example/", date="2021-01-01T00:00:00Z",
        extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    r = post_merge(left, right_resp + right_rev)
    assert r.status_code == 200, r.text
    new_id = r.json()["renamed"][0]["new_id"]
    records = replay_records(merged_bytes(r))
    assert records[2]["body_source_id"] == new_id
    assert bodies(records)[2] == b"RIGHT"


def test_merge_multilevel_chain_with_renamed_intermediate():
    # 右包多级链 v1 -> v2 -> a，其中 v2 与 a 均与左包冲突：逐级改写
    left = (raw_record("<urn:uuid:v2>", block=http_block(body=b"L-v2"))
            + raw_record("<urn:uuid:a>", block=http_block(body=b"L-a"),
                         uri="http://a.example/"))
    right_a = raw_record("<urn:uuid:a>", block=http_block(body=b"R-ORIG"),
                         uri="http://a.example/")
    right_v2 = raw_record(
        "<urn:uuid:v2>", rec_type="revisit", block=b"",
        uri="http://v2.example/", date="2021-02-02T00:00:00Z",
        extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    right_v1 = raw_record(
        "<urn:uuid:v1>", rec_type="revisit", block=b"",
        uri="http://v1.example/", date="2021-01-01T00:00:00Z",
        extra_headers=b"WARC-Refers-To: <urn:uuid:v2>\r\n")
    r = post_merge(left, right_a + right_v2 + right_v1)
    assert r.status_code == 200, r.text
    renamed = {x["old_id"]: x["new_id"] for x in r.json()["renamed"]}
    assert set(renamed) == {"<urn:uuid:a>", "<urn:uuid:v2>"}

    records = replay_records(merged_bytes(r))
    by_id = {rec["record_id"]: rec for rec in records}
    # 左包记录不受影响
    assert base64.b64decode(by_id["<urn:uuid:a>"]["body_base64"]) == b"L-a"
    # 右包链：v1 -> 改名后的 v2 -> 改名后的 a，正文均为右包终点正文
    new_a, new_v2 = renamed["<urn:uuid:a>"], renamed["<urn:uuid:v2>"]
    assert base64.b64decode(by_id[new_a]["body_base64"]) == b"R-ORIG"
    assert base64.b64decode(by_id[new_v2]["body_base64"]) == b"R-ORIG"
    assert by_id[new_v2]["body_source_id"] == new_a
    assert base64.b64decode(by_id["<urn:uuid:v1>"]["body_base64"]) == b"R-ORIG"
    assert by_id["<urn:uuid:v1>"]["body_source_id"] == new_a


def test_merge_rewritten_header_line_preserves_formatting():
    # 被改写行只换值：字段名大小写、冒号前后空白与 LF 行尾均保留原样
    left_block = b"HTTP/1.1 200 OK\n\nhello\n"
    right_block = b"HTTP/1.1 200 OK\n\nworld\n"
    left = (b"WARC/1.1\nwarc-type: response\nwarc-record-id: <urn:uuid:a>\n"
            b"warc-date: 2020-01-01T00:00:00Z\nwarc-target-uri: http://e.com/\n"
            + f"content-length: {len(left_block)}\n".encode() + b"\n"
            + left_block + b"\n")
    right = (b"WARC/1.1\nWARC-Type : response\nWARC-Record-ID : urn:uuid:a\n"
             b"WARC-Date: 2020-01-01T00:00:00Z\nWARC-Target-URI: http://e.com/\n"
             + f"Content-Length: {len(right_block)}\n".encode() + b"\n"
             + right_block + b"\n")
    r = post_merge(left, right)
    assert r.status_code == 200, r.text
    new_id = r.json()["renamed"][0]["new_id"]
    merged = merged_bytes(r)
    assert (b"WARC-Record-ID : " + new_id.encode() + b"\n") in merged
    records = replay_records(merged)
    assert records[1]["record_id"] == new_id
    assert bodies(records) == [b"hello\n", b"world\n"]


def test_merge_left_chain_to_shared_id_unaffected():
    # 左包 revisit 引用左包自己的记录：右包同 ID 记录被改名，左包链不变
    left_a = raw_record("<urn:uuid:a>", block=http_block(body=b"LEFT"))
    left_v = raw_record(
        "<urn:uuid:v>", rec_type="revisit", block=b"",
        date="2021-01-01T00:00:00Z",
        extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    right_a = raw_record("<urn:uuid:a>", block=http_block(body=b"RIGHT"))
    r = post_merge(left_a + left_v, right_a)
    assert r.status_code == 200, r.text
    records = replay_records(merged_bytes(r))
    assert records[1]["record_id"] == "<urn:uuid:v>"
    assert records[1]["body_source_id"] == "<urn:uuid:a>"
    assert bodies(records)[:2] == [b"LEFT", b"LEFT"]


def test_merge_replay_equivalence_with_mapping():
    # 混合两包：各自多级链 + 冲突；合并包回放与原包逐一等价（来源 ID 随映射）
    left_a = raw_record("<urn:uuid:a>", block=http_block(body=b"LEFT"))
    left_v = raw_record(
        "<urn:uuid:v>", rec_type="revisit", block=b"",
        date="2021-01-01T00:00:00Z",
        extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    right_a = raw_record("<urn:uuid:a>", block=http_block(
        "HTTP/1.1 201 Created", body=b"RIGHT"), uri="http://r.example/")
    right_w = raw_record(
        "<urn:uuid:w>", rec_type="revisit", block=b"",
        uri="http://w.example/", date="2021-02-02T00:00:00Z",
        extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    left, right = left_a + left_v, right_a + right_w
    r = post_merge(left, right)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 4
    new_id = data["renamed"][0]["new_id"]

    left_recs = replay_records(left)
    right_recs = replay_records(right)
    merged_recs = replay_records(merged_bytes(r))
    assert [rec["record_id"] for rec in merged_recs] == [
        "<urn:uuid:a>", "<urn:uuid:v>", new_id, "<urn:uuid:w>"]
    # 左包记录逐字段一致
    for got, want in zip(merged_recs[:2], left_recs):
        for field in ("record_id", "record_type", "target_uri",
                      "capture_time", "http_status", "body_base64",
                      "body_source_id"):
            assert got[field] == want[field], field
    # 右包记录：状态/正文一致，记录 ID 与正文来源 ID 随映射替换
    assert merged_recs[2]["http_status"] == right_recs[0]["http_status"] == 201
    assert merged_recs[2]["body_base64"] == right_recs[0]["body_base64"]
    assert merged_recs[2]["body_source_id"] == new_id
    assert merged_recs[3]["http_status"] == right_recs[1]["http_status"]
    assert merged_recs[3]["body_base64"] == right_recs[1]["body_base64"]
    assert merged_recs[3]["body_source_id"] == new_id


# ---------- 续行（folding）字段值的完整改写 ----------


def folded_record(field_lines, rec_type="response", block=b"",
                  extra_headers=b"", uri="http://e.com/",
                  date="2020-01-01T00:00:00Z", nl=b"\r\n"):
    """字段值可折叠在续行上：field_lines 为 (字段名, [首行片段, 续行...]) 列表。

    续行按原样给出（须以空格/制表符开头）；CRLF/LF 行尾由 nl 控制。
    """
    out = b"WARC/1.1" + nl
    out += b"WARC-Type: " + rec_type.encode() + nl
    for name, parts in field_lines:
        first, *conts = parts
        out += name.encode() + b": " + first + nl
        for cont in conts:
            out += cont + nl
    out += b"WARC-Date: " + date.encode() + nl
    out += b"WARC-Target-URI: " + uri.encode() + nl
    out += f"Content-Length: {len(block)}".encode() + nl
    out += extra_headers + nl
    return out + block + nl + nl


def test_merge_folded_record_id_rewritten_as_whole_value():
    # 右包冲突记录的 WARC-Record-ID 值跨续行：整体收敛为新值，旧值不残留
    left = raw_record("<urn:uuid:a>", block=http_block(body=b"LEFT"))
    right = folded_record(
        [("WARC-Record-ID", [b"<urn:uuid:a", b" >"])],
        block=http_block(body=b"RIGHT"))
    # 输入本身可回放（续行被解析器拼回，规范化后即 urn:uuid:a）
    assert [r["record_id"] for r in replay_records(right)] == ["<urn:uuid:a >"]

    r = post_merge(left, right)
    assert r.status_code == 200, r.text
    new_id = r.json()["renamed"][0]["new_id"]
    merged = merged_bytes(r)

    # 完整字段值改写：首行是新值，续行连同旧值片段被清除
    assert f"WARC-Record-ID: {new_id}\r\n".encode() in merged
    assert b"<urn:uuid:a" not in merged.split(b"RIGHT")[0].split(b"LEFT")[-1]
    assert b" >\r\n" not in merged
    # 其余头与负载原样
    assert b"WARC-Type: response\r\n" in merged
    assert b"HTTP/1.1 200 OK\r\n\r\nRIGHT" in merged

    records = replay_records(merged)
    assert [rec["record_id"] for rec in records] == ["<urn:uuid:a>", new_id]
    assert [rec["order"] for rec in records] == [0, 1]
    assert [rec["http_status"] for rec in records] == [200, 200]
    assert bodies(records) == [b"LEFT", b"RIGHT"]
    assert records[1]["body_source_id"] == new_id


def test_merge_folded_refers_to_rewritten_as_whole_value():
    # 右包 revisit 的 WARC-Refers-To 写在制表符续行上：同步整体改写
    left = raw_record("<urn:uuid:a>", block=http_block(body=b"LEFT"))
    right_resp = raw_record("<urn:uuid:a>", block=http_block(body=b"RIGHT"))
    right_rev = folded_record(
        [("WARC-Record-ID", [b"<urn:uuid:v>"]),
         ("WARC-Refers-To", [b"<urn:uuid:a", b"\t>"])],
        rec_type="revisit", block=b"", uri="http://v.example/")
    r = post_merge(left, right_resp + right_rev)
    assert r.status_code == 200, r.text
    new_id = r.json()["renamed"][0]["new_id"]
    merged = merged_bytes(r)

    assert f"WARC-Refers-To: {new_id}\r\n".encode() in merged
    assert b"\t>\r\n" not in merged  # 续行旧值已清除
    records = replay_records(merged)
    assert [rec["record_id"] for rec in records] == [
        "<urn:uuid:a>", new_id, "<urn:uuid:v>"]
    assert bodies(records) == [b"LEFT", b"RIGHT", b"RIGHT"]
    assert records[2]["body_source_id"] == new_id


def test_merge_folded_headers_lf_line_endings():
    # 纯 LF 行尾 + 空格续行：新行沿用 LF，记录仍可回放
    left_block = b"HTTP/1.1 200 OK\n\nLEFT\n"
    right_block = b"HTTP/1.1 200 OK\n\nRIGHT\n"
    left = (b"WARC/1.1\nwarc-type: response\n"
            b"warc-record-id: <urn:uuid:a>\n"
            b"warc-date: 2020-01-01T00:00:00Z\n"
            b"warc-target-uri: http://e.com/\n"
            + f"content-length: {len(left_block)}\n".encode() + b"\n"
            + left_block + b"\n")
    right = folded_record(
        [("warc-record-id", [b"<urn:uuid:a", b" >"])],
        block=right_block, nl=b"\n")
    r = post_merge(left, right)
    assert r.status_code == 200, r.text
    new_id = r.json()["renamed"][0]["new_id"]
    merged = merged_bytes(r)
    assert (b"warc-record-id: " + new_id.encode() + b"\n") in merged
    assert b" >\n" not in merged
    records = replay_records(merged)
    assert [rec["record_id"] for rec in records] == ["<urn:uuid:a>", new_id]
    assert bodies(records) == [b"LEFT\n", b"RIGHT\n"]


def test_merge_rewrite_keeps_unrelated_folded_field_verbatim():
    # 记录被改名时，相邻的其他折叠字段（含多续行）逐字节保留
    left = raw_record("<urn:uuid:a>", block=http_block(body=b"LEFT"))
    right = folded_record(
        [("WARC-Record-ID", [b"<urn:uuid:a", b" >"]),
         ("X-Folded", [b"part1", b" part2", b"\tpart3"]),
         ("WARC-Target-URI", [b"http://e.com/"])],
        block=http_block(body=b"RIGHT"))
    r = post_merge(left, right)
    assert r.status_code == 200, r.text
    merged = merged_bytes(r)
    assert b"X-Folded: part1\r\n part2\r\n\tpart3\r\n" in merged
    records = replay_records(merged)
    assert bodies(records) == [b"LEFT", b"RIGHT"]


def test_merge_no_conflict_keeps_folded_record_bytes_verbatim():
    # 无冲突：带续行的右包记录不做任何改写，字节与原记录完全一致
    right = folded_record(
        [("WARC-Record-ID", [b"<urn:uuid:b", b" >"])],
        block=http_block(body=b"bb"))
    left = raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))
    r = post_merge(left, right)
    assert r.status_code == 200, r.text
    assert r.json()["renamed"] == []
    merged = merged_bytes(r)
    assert merged == (left + right)[: -len(b"\r\n\r\n")]
    records = replay_records(merged)
    assert [rec["record_id"] for rec in records] == ["<urn:uuid:a>", "<urn:uuid:b >"]
    assert bodies(records) == [b"aa", b"bb"]


def test_merge_folded_refers_to_only_when_id_not_conflicting():
    # revisit 自身 ID 不冲突，仅其折叠的 Refers-To 指向改名记录：只改该字段
    left = raw_record("<urn:uuid:a>", block=http_block(body=b"LEFT"))
    right_resp = raw_record("<urn:uuid:a>", block=http_block(body=b"RIGHT"))
    # revisit 自身 ID 折叠成 <urn:uuid:v>（续行以空格开头才是折叠），
    # 规范化为 urn:uuid:v>，与左包 urn:uuid:a 不冲突
    right_rev = folded_record(
        [("WARC-Record-ID", [b"<urn:uuid:v", b" >"]),
         ("WARC-Refers-To", [b"<urn:uuid:a", b" >"])],
        rec_type="revisit", block=b"", uri="http://v.example/")
    r = post_merge(left, right_resp + right_rev)
    assert r.status_code == 200, r.text
    new_id = r.json()["renamed"][0]["new_id"]
    merged = merged_bytes(r)
    # Record-ID 无冲突，其续行原样保留
    assert b"WARC-Record-ID: <urn:uuid:v\r\n >\r\n" in merged
    # Refers-To 指向改名记录，整体改写
    assert f"WARC-Refers-To: {new_id}\r\n".encode() in merged
    records = replay_records(merged)
    assert records[2]["record_id"] == "<urn:uuid:v >"
    assert records[2]["body_source_id"] == new_id
    assert bodies(records)[2] == b"RIGHT"


def test_merge_folded_deterministic_across_requests():
    left = raw_record("<urn:uuid:a>", block=http_block(body=b"LEFT"))
    right = folded_record(
        [("WARC-Record-ID", [b"<urn:uuid:a", b" >"])],
        block=http_block(body=b"RIGHT"))
    r1, r2 = post_merge(left, right), post_merge(left, right)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()


# ---------- 仅含空格/制表符的 WARC 头结束行（warcio 口径） ----------


def raw_head_record(head_fields, block, terminator, rec_id=None,
                    rec_type="response", uri="http://e.com/"):
    """手工拼装：WARC 头字段序列 + 指定结束行（可仅含空格/制表符）+ 记录块。

    head_fields 为已含行尾的原始字段行；Content-Length 由调用方自行放入
    （或缺席以模拟块延伸至 EOF 的末记录）。
    """
    out = b"WARC/1.1\r\nWARC-Type: " + rec_type.encode() + b"\r\n"
    if rec_id is not None:
        out += b"WARC-Record-ID: " + rec_id + b"\r\n"
    out += b"WARC-Date: 2020-01-01T00:00:00Z\r\n"
    out += b"WARC-Target-URI: " + uri.encode() + b"\r\n"
    out += head_fields
    out += terminator
    return out + block


@pytest.mark.parametrize("terminator", [b" \r\n", b"\t\r\n", b"  \t \r\n",
                                        b" \n", b"\t\n", b"\r\n", b"\n"])
def test_merge_conflict_id_adjacent_to_whitespace_terminator(terminator):
    # 右包冲突的 WARC-Record-ID 是末个字段，紧邻仅含空格/制表符的结束行：
    # 结束行不是该字段的续行，改写必须保留它与 HTTP 状态行的边界
    left = raw_record("<urn:uuid:a>", block=http_block(
        "HTTP/1.1 200 OK", [("X-Left", "1")], body=b"LEFT"))
    block = http_block("HTTP/1.1 404 Not Found",
                       [("X-Right", "2")], body=b"RIGHT")
    fields = (b"Content-Length: " + str(len(block)).encode() + b"\r\n"
              + b"WARC-Record-ID: <urn:uuid:a>\r\n")
    right = raw_head_record(fields, block, terminator)
    assert replay_records(right)[0]["http_status"] == 404  # 输入本身合法

    r = post_merge(left, right)
    assert r.status_code == 200, r.text
    new_id = r.json()["renamed"][0]["new_id"]
    merged = merged_bytes(r)

    # 结束行原样保留（不被当成续行删除），HTTP 头与正文原字节保留
    assert (b"WARC-Record-ID: " + new_id.encode() + b"\r\n" + terminator
            + b"HTTP/1.1 404 Not Found\r\nX-Right: 2\r\n\r\nRIGHT") in merged
    # 旧 ID 只剩左包那一处（右包头中不再残留）
    assert merged.count(b"WARC-Record-ID: <urn:uuid:a>\r\n") == 1

    records = replay_records(merged)
    assert [rec["record_id"] for rec in records] == [
        "<urn:uuid:a>", new_id]
    assert [rec["http_status"] for rec in records] == [200, 404]
    assert bodies(records) == [b"LEFT", b"RIGHT"]
    assert records[1]["body_source_id"] == new_id


def test_merge_folded_target_plus_whitespace_terminator():
    # 目标字段既有真实续行、又紧邻仅含空格的结束行：只删除真实续行，
    # 结束行逐字节保留
    left = raw_record("<urn:uuid:a>", block=http_block(body=b"LEFT"))
    block = http_block(body=b"RIGHT")
    fields = (b"Content-Length: " + str(len(block)).encode() + b"\r\n"
              b"WARC-Record-ID: <urn:uuid:a\r\n >\r\n")  # 真实续行
    terminator = b" \t \r\n"
    right = raw_head_record(fields, block, terminator)
    r = post_merge(left, right)
    assert r.status_code == 200, r.text
    new_id = r.json()["renamed"][0]["new_id"]
    merged = merged_bytes(r)
    assert (b"WARC-Record-ID: " + new_id.encode() + b"\r\n" + terminator
            + b"HTTP/1.1 200 OK\r\n\r\nRIGHT") in merged
    assert b" >\r\n" not in merged  # 真实续行中的旧值片段已清除
    records = replay_records(merged)
    assert bodies(records) == [b"LEFT", b"RIGHT"]


def test_merge_whitespace_terminator_keeps_unrelated_continuation_verbatim():
    # 无冲突时含空格结束行与其他折叠字段的记录整体逐字节保留
    left = raw_record("<urn:uuid:a>", block=http_block(body=b"LEFT"))
    block = http_block(body=b"RIGHT")
    fields = (b"WARC-Record-ID: <urn:uuid:c>\r\n"
              b"WARC-Date: 2020-01-01T00:00:00Z\r\n"
              b"Content-Length: " + str(len(block)).encode() + b"\r\n"
              b"WARC-Target-URI: http://e.com/\r\n part2\r\n")
    terminator = b" \t \r\n"
    right = b"WARC/1.1\r\nWARC-Type: response\r\n" + fields + terminator \
        + block + b"\r\n\r\n"
    assert replay_records(right)  # 输入本身合法
    r = post_merge(left, right)
    assert r.status_code == 200, r.text
    assert r.json()["renamed"] == []
    # 右包记录逐字节保留；仅去掉记录间空行（右包本身以结束行收尾，
    # 其后的记录分隔 \r\n\r\n 不在 span 内）
    assert merged_bytes(r) == (left + right)[: -len(b"\r\n\r\n")]


@pytest.mark.parametrize("terminator", [b" \r\n", b"\t\r\n", b" \n"])
def test_merge_refers_to_adjacent_to_whitespace_terminator(terminator):
    # revisit 的 WARC-Refers-To 紧邻仅含空白的结束行：同步改写且不吞边界
    left = raw_record("<urn:uuid:a>", block=http_block(body=b"LEFT"))
    right_resp = raw_record("<urn:uuid:a>", block=http_block(body=b"RIGHT"))
    fields = (b"Content-Length: 0\r\n"
              b"WARC-Refers-To: <urn:uuid:a>\r\n")
    right_rev = raw_head_record(fields, b"", terminator,
                                rec_id=b"<urn:uuid:v>",
                                rec_type="revisit", uri="http://v.example/")
    r = post_merge(left, right_resp + right_rev)
    assert r.status_code == 200, r.text
    new_id = r.json()["renamed"][0]["new_id"]
    merged = merged_bytes(r)
    assert (b"WARC-Refers-To: " + new_id.encode() + b"\r\n" + terminator
            in merged)
    records = replay_records(merged)
    assert [rec["record_id"] for rec in records] == [
        "<urn:uuid:a>", new_id, "<urn:uuid:v>"]
    assert records[2]["body_source_id"] == new_id
    assert bodies(records) == [b"LEFT", b"RIGHT", b"RIGHT"]


def test_merge_whitespace_terminator_truncated_block_still_422():
    # 结束行合法但块被截断：仍是畸形输入 → 422，不交付
    left = raw_record("<urn:uuid:a>", block=http_block(body=b"LEFT"))
    fields = (b"Content-Length: 100\r\n"
              b"WARC-Record-ID: <urn:uuid:a>\r\n")
    right = raw_head_record(fields, b"HTTP/1.1 200 OK\r\n\r\nshort",
                            b" \r\n")
    r = post_merge(left, right)
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"


def test_merge_delivers_422_instead_of_unreplayable_result():
    # 兜底契约：交付前的再校验失败时返回 422 invalid_merged_warc，不给伪成功
    from app.merge import _validate_merged
    from app.replay import ReplayError, _RawRecord

    good = _RawRecord(
        order=0, record_id_raw="<urn:uuid:a>", record_id="urn:uuid:a",
        rec_type="response", target_uri="http://e.com/",
        date="2020-01-01T00:00:00Z", refers_to_raw=None, refers_to=None,
        body=b"x", http_status=200,
    )
    # 合并字节声称含 1 条记录，但再解析得到 0 条（无法回放）
    try:
        _validate_merged(b"", [good], [], {})
    except ReplayError as exc:
        assert exc.status_code == 422
        assert exc.code == "invalid_merged_warc"
    else:
        raise AssertionError("expected ReplayError")


# ---------- 左包末记录缺 Content-Length ----------


def test_merge_left_last_record_without_content_length():
    left = no_cl_record("<urn:uuid:a>")
    right = raw_record("<urn:uuid:b>", block=http_block(body=b"world"),
                       uri="http://b.example/")
    r = post_merge(left, right)
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2

    merged = merged_bytes(r)
    # 缺 Content-Length 的左包末记录被注入实际块长，右包记录不被吞掉
    assert b"Content-Length:" in merged
    for rec in ArchiveIterator(io.BytesIO(merged)):
        declared = int(rec.rec_headers.get_header("Content-Length"))
        rec.raw_stream.read()
        assert rec.raw_stream.tell() == declared

    records = replay_records(merged)
    assert [rec["record_id"] for rec in records] == [
        "<urn:uuid:a>", "<urn:uuid:b>"]
    # 左包记录还原正文与原包逐字节一致（原包块延伸至 EOF 的口径）
    original_left = replay_records(left)
    assert records[0]["body_base64"] == original_left[0]["body_base64"]
    assert bodies(records)[1] == b"world"


def test_merge_injected_content_length_is_actual_block_length():
    block = http_block(body=b"hello") + b"\r\n\r\n"
    left = no_cl_record("<urn:uuid:a>", block=block)
    right = raw_record("<urn:uuid:b>", block=http_block(body=b"b"),
                       uri="http://b.example/")
    r = post_merge(left, right)
    merged = merged_bytes(r)
    assert f"Content-Length: {len(block)}\r\n".encode() in merged
    # 注入只加头部一行，块字节不变
    assert merged.count(block) == 1


def test_merge_left_no_cl_last_after_normal_records():
    first = raw_record("<urn:uuid:f>", block=http_block(body=b"first"),
                       uri="http://f.example/")
    last = no_cl_record("<urn:uuid:a>")
    right = raw_record("<urn:uuid:b>", block=http_block(body=b"world"),
                       uri="http://b.example/")
    left = first + last
    r = post_merge(left, right)
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 3
    records = replay_records(merged_bytes(r))
    assert [rec["record_id"] for rec in records] == [
        "<urn:uuid:f>", "<urn:uuid:a>", "<urn:uuid:b>"]
    original = replay_records(left)
    assert records[1]["body_base64"] == original[1]["body_base64"]
    assert bodies(records)[2] == b"world"


def test_merge_left_no_cl_block_without_trailing_newline():
    # 缺 Content-Length 且块不以换行结尾（归档在块中间 EOF）
    left = no_cl_record("<urn:uuid:a>", block=http_block(body=b"hello"))
    right = raw_record("<urn:uuid:b>", block=http_block(body=b"world"),
                       uri="http://b.example/")
    r = post_merge(left, right)
    assert r.status_code == 200, r.text
    records = replay_records(merged_bytes(r))
    original = replay_records(left)
    assert records[0]["body_base64"] == original[0]["body_base64"]
    assert bodies(records) == [b"hello", b"world"]


def test_merge_left_no_cl_last_record_right_empty_no_injection():
    # 右包为空：左包末记录保持缺 Content-Length 的原样（块仍延伸至 EOF）
    left = no_cl_record("<urn:uuid:a>")
    r = post_merge(left, b"")
    assert r.status_code == 200, r.text
    merged = merged_bytes(r)
    assert b"Content-Length:" not in merged.split(b"\r\n\r\n")[0]
    records = replay_records(merged)
    original = replay_records(left)
    assert records[0]["body_base64"] == original[0]["body_base64"]


def test_merge_right_no_cl_last_record_stays_at_eof():
    # 右包末记录缺 Content-Length：它在合并包末尾，无需注入
    left = raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))
    right = no_cl_record("<urn:uuid:b>", uri="http://b.example/")
    r = post_merge(left, right)
    assert r.status_code == 200, r.text
    records = replay_records(merged_bytes(r))
    original_right = replay_records(right)
    assert records[1]["body_base64"] == original_right[0]["body_base64"]


def no_cl_record_terminator(rec_id, block, terminator,
                            uri="http://e.com/"):
    """缺 Content-Length 的末记录，WARC 头以指定结束行（可仅含空白）收尾。"""
    return (b"WARC/1.1\r\nWARC-Type: response\r\n"
            b"WARC-Record-ID: " + rec_id + b"\r\n"
            b"WARC-Date: 2020-01-01T00:00:00Z\r\n"
            b"WARC-Target-URI: " + uri.encode() + b"\r\n"
            + terminator + block)


@pytest.mark.parametrize("terminator", [b" \r\n", b"\t\r\n", b"  \r\n",
                                        b" \n", b"\t\n"])
def test_merge_no_cl_left_last_with_whitespace_terminator(terminator):
    # 左包末记录缺 Content-Length、头以仅含空白的行结束：补长须落在
    # WARC 头（结束行之前），按整个记录块计长，右包记录不被吞
    block = http_block("HTTP/1.1 200 OK", [("X-In-Block", "z")],
                       body=b"hello")
    left = no_cl_record_terminator(b"<urn:uuid:a>", block, terminator)
    right = raw_record("<urn:uuid:b>", block=http_block(body=b"world"),
                       uri="http://b.example/")
    original = replay_records(left)

    r = post_merge(left, right)
    assert r.status_code == 200, r.text
    merged = merged_bytes(r)

    # 注入行在 WARC 头内（结束行之前），值为整个记录块长度；注入行沿用
    # 结束行的换行风格（LF 结束行 -> LF 注入行）；结束行与 HTTP 头保留
    inject_nl = b"\n" if terminator.endswith(b"\n") and not terminator.endswith(
        b"\r\n") else b"\r\n"
    assert (b"Content-Length: " + str(len(block)).encode()
            + inject_nl + terminator
            + b"HTTP/1.1 200 OK\r\nX-In-Block: z\r\n\r\nhello"
            + b"\r\n\r\nWARC/1.1\r\n") in merged
    for rec in ArchiveIterator(io.BytesIO(merged)):
        declared = int(rec.rec_headers.get_header("Content-Length"))
        rec.raw_stream.read()
        assert rec.raw_stream.tell() == declared

    records = replay_records(merged)
    assert [x["record_id"] for x in records] == [
        "<urn:uuid:a>", "<urn:uuid:b>"]
    assert records[0]["body_base64"] == original[0]["body_base64"]
    assert bodies(records) == [b"hello", b"world"]


def test_merge_no_cl_left_last_lf_whitespace_terminator_after_normal():
    # 普通记录之后的缺长末记录、纯 LF + 空格结束行：补长换行符沿用 LF
    first = raw_record("<urn:uuid:f>", block=http_block(body=b"first"),
                       uri="http://f.example/")
    block = b"HTTP/1.1 200 OK\n\nhello"
    last = (b"WARC/1.1\nWARC-Type: response\nWARC-Record-ID: <urn:uuid:a>\n"
            b"WARC-Date: 2020-01-01T00:00:00Z\n"
            b"WARC-Target-URI: http://e.com/\n \n" + block)
    right = raw_record("<urn:uuid:b>", block=http_block(body=b"world"),
                       uri="http://b.example/")
    r = post_merge(first + last, right)
    assert r.status_code == 200, r.text
    merged = merged_bytes(r)
    assert (b"Content-Length: " + str(len(block)).encode() + b"\n \n"
            in merged)
    records = replay_records(merged)
    assert [x["record_id"] for x in records] == [
        "<urn:uuid:f>", "<urn:uuid:a>", "<urn:uuid:b>"]
    assert bodies(records) == [b"first", b"hello", b"world"]


# ---------- 体积限制与 422 错误 ----------


def test_merge_merged_exceeds_limit_422():
    left = raw_record("<urn:uuid:a>",
                      block=http_block(body=b"x" * (1024 * 1024)))
    right = raw_record("<urn:uuid:b>",
                       block=http_block(body=b"y" * (1024 * 1024 + 512 * 1024)),
                       uri="http://b.example/")
    # 各自 ≤ 2 MiB，合并后 > 2 MiB
    assert len(left) <= MAX_DECODED_BYTES
    assert len(right) <= MAX_DECODED_BYTES
    r = post_merge(left, right)
    assert r.status_code == 422
    data = r.json()
    assert data["error"] == "payload_too_large"
    assert data["max_bytes"] == MAX_DECODED_BYTES
    assert data["decoded_size"] > MAX_DECODED_BYTES


def test_merge_merged_exactly_at_limit_ok():
    # 合并后恰好 2 MiB 合法
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))
    # 直接解出右包正文长度：正文 ~2 MiB，其 Content-Length 为 7 位数字，
    # 先按 7 位计算再验证自洽（probe 的块长 19、CL 为 2 位数字）
    probe = raw_record("<urn:uuid:b>", block=http_block(body=b""),
                       uri="http://b.example/")
    body_len = (MAX_DECODED_BYTES - len((a + probe)[: -len(b"\r\n\r\n")])
                + len("19") - 7)
    assert len(str(19 + body_len)) == 7
    b = raw_record("<urn:uuid:b>", block=http_block(body=b"b" * body_len),
                   uri="http://b.example/")
    r = post_merge(a, b)
    assert r.status_code == 200, r.text
    assert len(merged_bytes(r)) == MAX_DECODED_BYTES


def test_merge_input_too_large():
    blob = b"x" * (MAX_DECODED_BYTES + 1)
    enc = base64.b64encode(blob).decode()
    good = base64.b64encode(
        raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))).decode()
    r = client.post("/v1/merge",
                    json={"left_warc": enc, "right_warc": good})
    assert r.status_code == 422
    assert r.json()["error"] == "payload_too_large"
    r = client.post("/v1/merge",
                    json={"left_warc": good, "right_warc": enc})
    assert r.status_code == 422
    assert r.json()["error"] == "payload_too_large"


def test_merge_invalid_base64():
    good = base64.b64encode(
        raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))).decode()
    r = client.post("/v1/merge",
                    json={"left_warc": "!!!not-base64!!!",
                          "right_warc": good})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_base64"
    r = client.post("/v1/merge",
                    json={"left_warc": good,
                          "right_warc": "!!!not-base64!!!"})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_base64"


def test_merge_missing_fields_422():
    enc = base64.b64encode(
        raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))).decode()
    assert client.post("/v1/merge",
                       json={"left_warc": enc}).status_code == 422
    assert client.post("/v1/merge",
                       json={"right_warc": enc}).status_code == 422
    assert client.post("/v1/merge", json={}).status_code == 422


def test_merge_validates_each_archive_independently():
    good = raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))
    # 左包畸形
    r = post_merge(b"this is not a warc file at all", good)
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"
    # 右包畸形
    r = post_merge(good, b"this is not a warc file at all")
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"
    # 右包内部重复 ID → 422（跨包同 ID 不是重复，而是改名）
    dup = good + raw_record("<urn:uuid:a>", block=http_block(body=b"dup"))
    r = post_merge(good, dup)
    assert r.status_code == 422
    assert r.json()["error"] == "duplicate_record_id"
    # 右包 revisit 指向只在左包存在的 ID：右包自身校验即失败（不自包含）
    dangling = raw_record(
        "<urn:uuid:v>", rec_type="revisit", block=b"",
        extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    r = post_merge(good, dangling)
    assert r.status_code == 422
    assert r.json()["error"] == "target_not_found"


def test_merge_left_validated_before_right():
    # 两包都有错时先报左包（先左后右）
    a = raw_record("<urn:uuid:a>", block=http_block(body=b"aa"))
    left_dup = a + raw_record("<urn:uuid:a>", block=http_block(body=b"dup"))
    r = post_merge(left_dup, b"not a warc")
    assert r.status_code == 422
    assert r.json()["error"] == "duplicate_record_id"
