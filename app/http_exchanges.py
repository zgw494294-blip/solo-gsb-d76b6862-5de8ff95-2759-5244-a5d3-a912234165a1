"""HTTP 交换重建（POST /v1/http-exchanges）核心。

接收 Base64 WARC，按**显式关联**重建 ``request`` 与
``response``/``revisit`` 的 HTTP 交换：

- 归档完整经过与 /v1/replay 相同的解码（2 MiB 上限、gzip 嗅探、截断检查）
  与 response/revisit 的结构、revisit 引用链校验；此外 ``request`` 记录
  一并解析：它们与 response/revisit 一样必须带 WARC-Record-ID
  （missing_record_id），ID 唯一性检查覆盖 request/response/revisit
  **全集**（duplicate_record_id），且 request 记录必须是合法的 HTTP 请求
  （request_without_http_headers，与 response 的同类校验对应）；
- 配对**只依据**显式的 ``WARC-Concurrent-To``：request 与
  response/revisit **任一侧**声明即建立配对，按现有 ID 规范化规则
  （去空白、去尖括号）指向另一侧的 WARC-Record-ID，**允许前向引用**；
  仅 URL 或抓取时间相同不构成配对；warcinfo/metadata 等其余类型不参与，
  指向它们等同于关联目标不存在；
- 合法配对为 request ↔ response/revisit。以下情形返回 422 并指出相关
  ID：同一记录涉及多个伙伴（``multiple_partners``）、双方指向不同伙伴
  （``inconsistent_partners``）、相关 ID 重复（``duplicate_record_id``）、
  关联目标不存在（``concurrent_target_not_found``）、类型不符
  （``concurrent_type_mismatch``）；
- 响应按 **request 在归档中的顺序**返回各交换的配对双方 ID、目标 URL
  （取响应侧记录的 WARC-Target-URI）、HTTP 请求方法、请求目标与响应
  状态（revisit 自身无 HTTP 头时为 null）；未配对的两类记录分别按归档
  原序列出 ID。
"""

from __future__ import annotations

from .replay import (
    ReplayError,
    _RawRecord,
    decode_warc_payload,
    parse_warc,
    validate_records,
)

# 交换的“响应侧”类型
_RESPONSE_TYPES = ("response", "revisit")


def _compatible_types(type_a: str, type_b: str) -> bool:
    """配对必须恰好一侧为 request，另一侧为 response/revisit。"""
    if type_a == "request":
        return type_b in _RESPONSE_TYPES
    if type_b == "request":
        return type_a in _RESPONSE_TYPES
    return False


def http_exchanges(warc_b64: str) -> dict:
    """http-exchanges 入口；任何输入/结构问题抛 ReplayError（422）。"""
    data = decode_warc_payload(warc_b64)
    raws = parse_warc(data, include_requests=True)

    # 1) WARC-Record-ID 唯一性：重复判定覆盖 request/response/revisit
    #    全集（按规范化 ID 比较），错误形态与 /v1/replay 一致
    by_id: dict[str, _RawRecord] = {}
    for raw in raws:
        if raw.record_id in by_id:
            raise ReplayError(
                "duplicate_record_id",
                f"WARC-Record-ID {raw.record_id_raw} 在包内重复出现",
                record_id=raw.record_id_raw,
                first_record_order=by_id[raw.record_id].order,
                duplicate_record_order=raw.order,
            )
        by_id[raw.record_id] = raw

    # 2) response/revisit 沿用现有全部校验：HTTP 响应头、revisit 引用链
    validate_records([raw for raw in raws if raw.rec_type in _RESPONSE_TYPES])

    # 3) request 记录必须是合法 HTTP 请求（与 response_without_http_headers
    #    对应），否则无法给出请求方法与请求目标
    for raw in raws:
        if raw.rec_type == "request" and raw.http_method is None:
            raise ReplayError(
                "request_without_http_headers",
                f"request 记录 {raw.record_id_raw} 缺少有效的 HTTP 请求头"
                "或请求行",
                record_id=raw.record_id_raw,
                record_order=raw.order,
            )

    # 4) 关联目标存在性与类型检查：按归档顺序、记录内按头字段出现顺序，
    #    首个错误即返回（允许前向引用——此时全部记录已解析完毕）
    for raw in raws:
        for norm, raw_value in zip(raw.concurrent_to, raw.concurrent_to_raw):
            target = by_id.get(norm)
            if target is None:
                raise ReplayError(
                    "concurrent_target_not_found",
                    f"记录 {raw.record_id_raw} 通过 WARC-Concurrent-To 关联的 "
                    f"{raw_value} 在包内不存在",
                    record_id=raw.record_id_raw,
                    missing_id=raw_value,
                )
            if not _compatible_types(raw.rec_type, target.rec_type):
                raise ReplayError(
                    "concurrent_type_mismatch",
                    f"记录 {raw.record_id_raw}（{raw.rec_type}）与 "
                    f"{target.record_id_raw}（{target.rec_type}）不能构成 "
                    "HTTP 交换：配对必须为 request 与 response/revisit",
                    record_id=raw.record_id_raw,
                    record_type=raw.rec_type,
                    target_id=target.record_id_raw,
                    target_type=target.rec_type,
                )

    # 各记录声明的伙伴（规范化 ID，同值重复出现自动去重，保持声明顺序）
    declared: dict[str, list[str]] = {}
    for raw in raws:
        unique: list[str] = []
        for norm in raw.concurrent_to:
            if norm not in unique:
                unique.append(norm)
        declared[raw.record_id] = unique

    # 5) 双方指向不同伙伴：A 声明 B，而 B 也有声明但不是 A
    #    （B 声明多个伙伴的情形留给步骤 6 归为 multiple_partners）
    for raw in raws:
        partners = declared[raw.record_id]
        if len(partners) != 1:
            continue
        other = by_id[partners[0]]
        other_partners = declared[other.record_id]
        if len(other_partners) == 1 and other_partners[0] != raw.record_id:
            raise ReplayError(
                "inconsistent_partners",
                f"记录 {raw.record_id_raw} 声明伙伴 {other.record_id_raw}，"
                "但后者声明的伙伴是 "
                f"{by_id[other_partners[0]].record_id_raw}，双方指向不同伙伴",
                record_id=raw.record_id_raw,
                partner_id=other.record_id_raw,
                partner_points_to=by_id[other_partners[0]].record_id_raw,
            )

    # 6) 同一记录涉及多个伙伴：自己声明与被他人声明合计超过一个不同伙伴
    inbound: dict[str, set[str]] = {raw.record_id: set() for raw in raws}
    for raw in raws:
        for norm in declared[raw.record_id]:
            inbound[norm].add(raw.record_id)
    for raw in raws:
        partners = set(declared[raw.record_id]) | inbound[raw.record_id]
        if len(partners) > 1:
            ordered = sorted(partners, key=lambda p: by_id[p].order)
            raise ReplayError(
                "multiple_partners",
                f"记录 {raw.record_id_raw} 涉及多个伙伴："
                + "、".join(by_id[p].record_id_raw for p in ordered),
                record_id=raw.record_id_raw,
                partners=[by_id[p].record_id_raw for p in ordered],
            )

    # 7) 建立配对：经过上述校验，每条记录至多有一个伙伴；双方互指时两个
    #    方向收敛为同一对
    partner_of: dict[str, str] = {}
    for raw in raws:
        for norm in declared[raw.record_id]:
            partner_of[raw.record_id] = norm
            partner_of[norm] = raw.record_id

    # 8) 输出：配对按 request 归档序；未配对两类分别按归档原序列出
    pairs: list[dict] = []
    unpaired_requests: list[str] = []
    unpaired_responses: list[str] = []
    for raw in raws:
        mate_id = partner_of.get(raw.record_id)
        if raw.rec_type == "request":
            if mate_id is None:
                unpaired_requests.append(raw.record_id_raw)
                continue
            response_rec = by_id[mate_id]
            pairs.append(
                {
                    "request_id": raw.record_id_raw,
                    "response_id": response_rec.record_id_raw,
                    "target_uri": response_rec.target_uri,
                    "method": raw.http_method,
                    "request_target": raw.http_request_target,
                    "http_status": response_rec.http_status,
                }
            )
        elif mate_id is None:
            unpaired_responses.append(raw.record_id_raw)

    return {
        "count": len(pairs),
        "pairs": pairs,
        "unpaired_requests": unpaired_requests,
        "unpaired_responses": unpaired_responses,
    }
