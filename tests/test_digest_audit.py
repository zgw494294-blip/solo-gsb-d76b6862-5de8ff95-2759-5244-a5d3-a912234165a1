"""POST /v1/digest-audit 端到端测试：摘要核验、格式错误与结构校验复用。"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io

from fastapi.testclient import TestClient
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import BufferWARCWriter

from app.main import app

client = TestClient(app)


# ---------- WARC 构造与摘要辅助 ----------


def b32(data: bytes, algo: str = "sha1") -> str:
    """无填充 Base32 摘要（与接口支持的声明形式一致）。"""
    digest = hashlib.new(algo, data).digest()
    return base64.b32encode(digest).decode("ascii").rstrip("=")


def http_block(status_line="HTTP/1.1 200 OK", headers=None, body=b""):
    h = status_line.encode() + b"\r\n"
    for k, v in (headers or []):
        h += f"{k}: {v}\r\n".encode()
    return h + b"\r\n" + body


def record_bytes(rec_id, block, rec_type="response", extra_headers=b"",
                 date="2020-01-01T00:00:00Z", uri="http://example.com/",
                 with_content_length=True, trailing_separator=True):
    cl = (
        f"Content-Length: {len(block)}\r\n".encode()
        if with_content_length
        else b""
    )
    return (
        f"WARC/1.1\r\nWARC-Type: {rec_type}\r\n"
        f"WARC-Record-ID: {rec_id}\r\nWARC-Date: {date}\r\n"
        f"WARC-Target-URI: {uri}\r\n".encode()
        + extra_headers + cl + b"\r\n" + block
        + (b"\r\n\r\n" if trailing_separator else b"")
    )


def digest_headers(block: bytes, payload: bytes, algo="sha1",
                   block_algo=None, payload_algo=None) -> bytes:
    """生成与给定块/正文匹配的 WARC-Block/Payload-Digest 头。"""
    ba = block_algo or algo
    pa = payload_algo or algo
    return (
        f"WARC-Block-Digest: {ba}:{b32(block, ba)}\r\n"
        f"WARC-Payload-Digest: {pa}:{b32(payload, pa)}\r\n"
    ).encode()


def post(warc_bytes, **kw):
    return client.post(
        "/v1/digest-audit",
        json={"warc": base64.b64encode(warc_bytes).decode()},
        **kw,
    )


# ---------- 正常路径 ----------


def test_match_sha1_block_and_payload():
    body = b"hello"
    block = http_block(body=body)
    warc = record_bytes("<urn:uuid:a>", block,
                        extra_headers=digest_headers(block, body))
    r = post(warc)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 1
    assert data["mismatch_count"] == 0
    assert data["records"] == [
        {"order": 0, "record_id": "<urn:uuid:a>",
         "block_digest": "match", "payload_digest": "match"}
    ]


def test_match_sha256():
    body = b"hello sha256"
    block = http_block(body=body)
    warc = record_bytes("<urn:uuid:a>", block,
                        extra_headers=digest_headers(block, body,
                                                     algo="sha256"))
    r = post(warc)
    assert r.status_code == 200, r.text
    rec = r.json()["records"][0]
    assert rec["block_digest"] == "match"
    assert rec["payload_digest"] == "match"


def test_mixed_algorithms_per_field():
    body = b"mixed"
    block = http_block(body=body)
    warc = record_bytes(
        "<urn:uuid:a>", block,
        extra_headers=digest_headers(block, body, block_algo="sha1",
                                     payload_algo="sha256"),
    )
    r = post(warc)
    assert r.status_code == 200, r.text
    rec = r.json()["records"][0]
    assert rec["block_digest"] == "match"
    assert rec["payload_digest"] == "match"


def test_case_insensitive_algorithm_and_base32():
    body = b"case"
    block = http_block(body=body)
    headers = (
        f"WARC-Block-Digest: SHA1:{b32(block).lower()}\r\n"
        f"WARC-Payload-Digest: Sha256:{b32(body, 'sha256').lower()}\r\n"
    ).encode()
    r = post(record_bytes("<urn:uuid:a>", block, extra_headers=headers))
    assert r.status_code == 200, r.text
    rec = r.json()["records"][0]
    assert rec["block_digest"] == "match"
    assert rec["payload_digest"] == "match"


def test_absent_digest_headers():
    block = http_block(body=b"hello")
    r = post(record_bytes("<urn:uuid:a>", block))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["mismatch_count"] == 0
    rec = data["records"][0]
    assert rec["block_digest"] == "absent"
    assert rec["payload_digest"] == "absent"


def test_block_digest_covers_http_headers_not_warc_headers():
    # 同一 HTTP 块、不同 WARC 头（WARC-Date 不同）：同一个块摘要都匹配，
    # 说明 Block 摘要含 HTTP 头、不含 WARC 头。
    body = b"same-block"
    block = http_block(headers=[("Content-Type", "text/plain")], body=body)
    headers = (
        f"WARC-Block-Digest: sha1:{b32(block)}\r\n"
        f"WARC-Payload-Digest: sha1:{b32(body)}\r\n"
    ).encode()
    warc = (
        record_bytes("<urn:uuid:a>", block, date="2020-01-01T00:00:00Z",
                     extra_headers=headers)
        + record_bytes("<urn:uuid:b>", block, date="2021-06-01T00:00:00Z",
                       extra_headers=headers)
    )
    r = post(warc)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["mismatch_count"] == 0
    assert [rec["block_digest"] for rec in data["records"]] == ["match", "match"]


def test_mismatch_does_not_block_later_records():
    good_body = b"good"
    good_block = http_block(body=good_body)
    bad_block = http_block(body=b"bad")
    warc = (
        record_bytes(
            "<urn:uuid:bad>", bad_block,
            extra_headers=digest_headers(good_block, good_body),  # 张冠李戴
        )
        + record_bytes(
            "<urn:uuid:good>", good_block,
            extra_headers=digest_headers(good_block, good_body),
        )
    )
    r = post(warc)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 2
    # 第一条 block 失配、payload 失配（摘要按 good_body 算的）；第二条全匹配
    assert data["records"][0]["block_digest"] == "mismatch"
    assert data["records"][0]["payload_digest"] == "mismatch"
    assert data["records"][1]["block_digest"] == "match"
    assert data["records"][1]["payload_digest"] == "match"
    assert data["mismatch_count"] == 2


def test_mismatch_count_counts_each_field():
    body = b"real"
    block = http_block(body=body)
    other = b"other"
    headers = (
        f"WARC-Block-Digest: sha1:{b32(http_block(body=other))}\r\n"
        f"WARC-Payload-Digest: sha1:{b32(body)}\r\n"
    ).encode()
    r = post(record_bytes("<urn:uuid:a>", block, extra_headers=headers))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["records"][0]["block_digest"] == "mismatch"
    assert data["records"][0]["payload_digest"] == "match"
    assert data["mismatch_count"] == 1


def test_revisit_payload_digest_uses_endpoint_body():
    body = b"ORIGINAL"
    resp_block = http_block(body=body)
    revisit_block = b""  # 无 HTTP 头、空块
    revisit_headers = (
        b"WARC-Refers-To: <urn:uuid:a>\r\n"
        # revisit 的 payload 摘要按链终点 response 的正文计算
        + f"WARC-Payload-Digest: sha1:{b32(body)}\r\n".encode()
        # revisit 自己的块为空：块摘要是空字节的摘要
        + f"WARC-Block-Digest: sha1:{b32(b'')}\r\n".encode()
    )
    warc = (
        record_bytes("<urn:uuid:a>", resp_block,
                     extra_headers=digest_headers(resp_block, body))
        + record_bytes("<urn:uuid:v>", revisit_block, rec_type="revisit",
                       date="2021-01-01T00:00:00Z",
                       extra_headers=revisit_headers)
    )
    r = post(warc)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["mismatch_count"] == 0
    assert data["records"][1] == {
        "order": 1, "record_id": "<urn:uuid:v>",
        "block_digest": "match", "payload_digest": "match",
    }


def test_revisit_multilevel_chain_payload_digest():
    body = b"CHAIN-END"
    resp_block = http_block(body=body)
    # v1 -> v2 -> a（response）；v1 的 payload 摘要仍按终点 a 的正文
    warc = (
        record_bytes("<urn:uuid:a>", resp_block)
        + record_bytes("<urn:uuid:v1>", b"", rec_type="revisit",
                       date="2021-01-01T00:00:00Z",
                       extra_headers=(
                           b"WARC-Refers-To: <urn:uuid:v2>\r\n"
                           + f"WARC-Payload-Digest: sha256:{b32(body, 'sha256')}\r\n".encode()
                       ))
        + record_bytes("<urn:uuid:v2>", b"", rec_type="revisit",
                       date="2021-02-02T00:00:00Z",
                       extra_headers=b"WARC-Refers-To: <urn:uuid:a>\r\n")
    )
    r = post(warc)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["records"][1]["payload_digest"] == "match"
    assert data["records"][2]["payload_digest"] == "absent"


def test_revisit_with_own_http_block():
    # revisit 自带 HTTP 头：Block 摘要仍按它自己的记录块计算，
    # Payload 摘要按链终点 response 的正文计算。
    body = b"endpoint-body"
    resp_block = http_block(body=body)
    revisit_block = http_block(headers=[("Content-Length", "0")], body=b"")
    revisit_headers = (
        b"WARC-Refers-To: <urn:uuid:a>\r\n"
        + f"WARC-Block-Digest: sha1:{b32(revisit_block)}\r\n".encode()
        + f"WARC-Payload-Digest: sha1:{b32(body)}\r\n".encode()
    )
    warc = (
        record_bytes("<urn:uuid:a>", resp_block)
        + record_bytes("<urn:uuid:v>", revisit_block, rec_type="revisit",
                       date="2021-01-01T00:00:00Z",
                       extra_headers=revisit_headers)
    )
    r = post(warc)
    assert r.status_code == 200, r.text
    rec = r.json()["records"][1]
    assert rec["block_digest"] == "match"
    assert rec["payload_digest"] == "match"


def test_missing_content_length_last_record_bounded_by_eof():
    body = b"eof-body"
    block = http_block(body=body)
    warc = record_bytes(
        "<urn:uuid:a>", block,
        extra_headers=digest_headers(block, body),
        with_content_length=False, trailing_separator=False,
    )
    r = post(warc)
    assert r.status_code == 200, r.text
    rec = r.json()["records"][0]
    assert rec["block_digest"] == "match"
    assert rec["payload_digest"] == "match"


def test_missing_content_length_trailing_bytes_belong_to_block():
    # 缺 Content-Length 的末记录以 EOF 为界：尾部空行也计入记录块
    body = b"eof-body-2"
    block = http_block(body=body)
    warc = record_bytes(
        "<urn:uuid:a>", block,
        extra_headers=digest_headers(block + b"\r\n\r\n", body + b"\r\n\r\n"),
        with_content_length=False, trailing_separator=True,
    )
    r = post(warc)
    assert r.status_code == 200, r.text
    rec = r.json()["records"][0]
    assert rec["block_digest"] == "match"
    assert rec["payload_digest"] == "match"


def test_whitespace_only_line_terminates_warc_headers():
    # 与 warcio 的头部解析一致：只含空白字符的行即 WARC 头终止行
    block = http_block(body=b"x")
    warc = (
        b"WARC/1.1\r\nWARC-Type: response\r\nWARC-Record-ID: <urn:uuid:a>\r\n"
        b"WARC-Date: 2020-01-01T00:00:00Z\r\n"
        b"WARC-Target-URI: http://example.com/\r\n"
        + f"Content-Length: {len(block)}\r\n".encode()
        + f"WARC-Block-Digest: sha1:{b32(block)}\r\n".encode()
        + b" \r\n"  # 纯空白行终止 WARC 头
        + block + b"\r\n\r\n"
    )
    r = post(warc)
    assert r.status_code == 200, r.text
    assert r.json()["records"][0]["block_digest"] == "match"


def test_empty_warc_returns_empty_list():
    r = client.post("/v1/digest-audit", json={"warc": ""})
    assert r.status_code == 200, r.text
    assert r.json() == {"count": 0, "mismatch_count": 0, "records": []}


def test_gzip_input():
    body = b"gzipped"
    block = http_block(body=body)
    warc = record_bytes("<urn:uuid:a>", block,
                        extra_headers=digest_headers(block, body))
    r = post(gzip.compress(warc))
    assert r.status_code == 200, r.text
    assert r.json()["records"][0]["block_digest"] == "match"


def test_digest_headers_on_skipped_record_types_are_ignored():
    warcinfo = (
        b"WARC/1.1\r\nWARC-Type: warcinfo\r\nWARC-Record-ID: <urn:uuid:w>\r\n"
        b"WARC-Date: 2020-01-01T00:00:00Z\r\n"
        b"WARC-Block-Digest: md5:not-checked\r\n"
        b"Content-Length: 4\r\n\r\ninfo\r\n\r\n"
    )
    body = b"hello"
    block = http_block(body=body)
    warc = warcinfo + record_bytes(
        "<urn:uuid:a>", block, extra_headers=digest_headers(block, body)
    )
    r = post(warc)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 1
    assert data["records"][0]["record_id"] == "<urn:uuid:a>"


def test_warcio_written_record_with_auto_digests():
    # warcio 的 WARCWriter 会自动写入 WARC-Block-Digest /
    # WARC-Payload-Digest（sha1、无填充 Base32）：其口径应与本接口一致
    body = b"warcio-body"
    http = StatusAndHeaders(
        "200 OK", [("Content-Type", "text/plain")], protocol="HTTP/1.1"
    )
    w = BufferWARCWriter(gzip=False)
    rec = w.create_warc_record(
        "http://example.com/", "response", http_headers=http,
        payload=io.BytesIO(body),
        warc_headers_dict={"WARC-Record-ID": "<urn:uuid:w>"},
    )
    w.write_record(rec)
    r = post(w.get_contents())
    assert r.status_code == 200, r.text
    rec_out = r.json()["records"][0]
    assert rec_out["block_digest"] == "match"
    assert rec_out["payload_digest"] == "match"


# ---------- 摘要格式/算法错误（422） ----------


def test_invalid_format_missing_colon():
    block = http_block(body=b"x")
    headers = b"WARC-Block-Digest: sha1ABC\r\n"
    r = post(record_bytes("<urn:uuid:a>", block, extra_headers=headers))
    assert r.status_code == 422
    detail = r.json()
    assert detail["error"] == "invalid_digest_format"
    assert detail["record_id"] == "<urn:uuid:a>"
    assert detail["field"] == "WARC-Block-Digest"


def test_invalid_format_bad_base32_characters():
    block = http_block(body=b"x")
    headers = b"WARC-Block-Digest: sha1:0189ABCD\r\n"  # 0/1/8/9 不在 Base32 字符集
    r = post(record_bytes("<urn:uuid:a>", block, extra_headers=headers))
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_digest_format"


def test_invalid_format_padded_base32_rejected():
    block = http_block(body=b"x")
    # sha1 摘要恰为 32 个 Base32 字符、无填充；sha256 的带填充形式以 = 结尾
    padded = base64.b32encode(hashlib.sha256(block).digest()).decode()
    assert padded.endswith("=")
    headers = f"WARC-Block-Digest: sha256:{padded}\r\n".encode()
    r = post(record_bytes("<urn:uuid:a>", block, extra_headers=headers))
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_digest_format"


def test_invalid_format_wrong_length_for_algorithm():
    block = http_block(body=b"x")
    # sha256 的 Base32 长度（52）安到 sha1 上：格式错误而非 mismatch
    headers = f"WARC-Block-Digest: sha1:{b32(block, 'sha256')}\r\n".encode()
    r = post(record_bytes("<urn:uuid:a>", block, extra_headers=headers))
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_digest_format"


def test_unsupported_algorithm():
    block = http_block(body=b"x")
    headers = f"WARC-Block-Digest: md5:{b32(block)[:32]}\r\n".encode()
    r = post(record_bytes("<urn:uuid:a>", block, extra_headers=headers))
    assert r.status_code == 422
    detail = r.json()
    assert detail["error"] == "unsupported_digest_algorithm"
    assert detail["record_id"] == "<urn:uuid:a>"
    assert detail["field"] == "WARC-Block-Digest"
    assert detail["algorithm"] == "md5"


def test_error_points_to_payload_field_and_later_record():
    body = b"ok"
    block = http_block(body=body)
    warc = (
        record_bytes("<urn:uuid:a>", block,
                     extra_headers=digest_headers(block, body))
        + record_bytes("<urn:uuid:b>", block,
                       extra_headers=b"WARC-Payload-Digest: sha1:!!!\r\n")
    )
    r = post(warc)
    assert r.status_code == 422
    detail = r.json()
    assert detail["error"] == "invalid_digest_format"
    assert detail["record_id"] == "<urn:uuid:b>"
    assert detail["field"] == "WARC-Payload-Digest"


# ---------- 复用既有 WARC 结构校验 ----------


def test_malformed_warc_still_422():
    r = post(b"this is not a warc")
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"


def test_invalid_base64_still_422():
    r = client.post("/v1/digest-audit", json={"warc": "!!!not-base64!!!"})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_base64"


def test_broken_revisit_chain_still_422():
    body = b"x"
    block = http_block(body=body)
    warc = (
        record_bytes("<urn:uuid:a>", block)
        + record_bytes("<urn:uuid:v>", b"", rec_type="revisit",
                       date="2021-01-01T00:00:00Z")  # 缺 WARC-Refers-To
    )
    r = post(warc)
    assert r.status_code == 422
    assert r.json()["error"] == "chain_broken"


def test_duplicate_record_id_still_422():
    block = http_block(body=b"x")
    r = post(record_bytes("<urn:uuid:a>", block)
             + record_bytes("<urn:uuid:a>", block))
    assert r.status_code == 422
    assert r.json()["error"] == "duplicate_record_id"
