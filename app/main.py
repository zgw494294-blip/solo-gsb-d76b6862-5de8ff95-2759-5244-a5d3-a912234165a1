"""FastAPI 应用：WARC 重访（revisit）还原、离线时点导航与快照对比服务。"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .capture_index import capture_index
from .digest_audit import digest_audit
from .extract import extract
from .http_exchanges import http_exchanges
from .member_pack import member_pack
from .merge import merge
from .navigate import navigate
from .page_assets import page_assets
from .replay import ReplayError, replay
from .snapshot_diff import snapshot_diff

app = FastAPI(
    title="WARC Replay API",
    version="1.9.0",
    description="接收 Base64 编码的自包含 WARC：/v1/replay 还原 response/revisit "
    "记录，/v1/extract 按记录 ID 导出子集（含引用链传递依赖），"
    "/v1/navigate 按时点离线还原页面（跟随重定向），"
    "/v1/snapshot-diff 对比两份归档在同一时点各自可见的 URL，"
    "/v1/page-assets 检查该时点页面的静态资源能否从同包归档还原，"
    "/v1/digest-audit 逐条核验记录的 WARC-Block-Digest 与 "
    "WARC-Payload-Digest，/v1/capture-index 按（URL、UTC 时刻、归档序号）"
    "分页列出抓取及其在解压后归档中的字节范围，/v1/merge 将两份归档的 "
    "response/revisit 记录按左包、右包原序合并为一份可回放归档（跨包 ID "
    "冲突时为右包冲突记录生成确定性新 ID），/v1/http-exchanges 按双方任一"
    "侧的 WARC-Concurrent-To 显式关联重建 request 与 response/revisit 的 "
    "HTTP 交换（允许前向引用，URL/时间相同不配对），/v1/member-pack 把 "
    "response/revisit 记录按归档原序逐记录打成独立 gzip member，生成可按"
    "记录定位的多 member gzip（固定 gzip 时间戳，输出确定）。",
)


class ReplayRequest(BaseModel):
    warc: str = Field(
        ...,
        description="Base64 编码的 WARC 数据（可带空白/换行），解码后不超过 2 MiB；"
        "空字符串表示空 WARC，返回空记录列表",
    )


class ReplayRecord(BaseModel):
    order: int = Field(..., description="在归档中的处理序号（从 0 开始）")
    record_id: str = Field(..., description="WARC-Record-ID 原始值")
    record_type: str = Field(..., description="记录类型：response 或 revisit")
    target_uri: Optional[str] = Field(None, description="WARC-Target-URI")
    capture_time: Optional[str] = Field(None, description="WARC-Date")
    http_status: Optional[int] = Field(
        None, description="HTTP 状态码；revisit 自身无 HTTP 头时为 null"
    )
    body_base64: str = Field(..., description="正文 Base64；空正文为空字符串")
    body_source_id: str = Field(
        ..., description="正文来源记录 ID（revisit 为链终点 response 的 ID）"
    )


class ReplayResponse(BaseModel):
    count: int
    records: list[ReplayRecord]


@app.exception_handler(ReplayError)
async def replay_error_handler(_request: Request, exc: ReplayError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.detail)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/replay", response_model=ReplayResponse, status_code=200)
async def post_replay(payload: ReplayRequest) -> dict[str, Any]:
    records = replay(payload.warc)
    return {"count": len(records), "records": records}


class ExtractRequest(BaseModel):
    warc: str = Field(
        ...,
        description="Base64 编码的 WARC 数据（可带空白/换行），解码后不超过 2 MiB",
    )
    record_ids: list[str] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="要导出的 WARC-Record-ID（1～100 个）；按与归档内 ID 相同的"
        "规则匹配（去空白、去尖括号），重复自动去重；未知 ID 返回 422",
    )


class ExtractResponse(BaseModel):
    included_ids: list[str] = Field(
        ..., description="实际纳入导出包的记录 ID（WARC-Record-ID 原始值），"
        "按归档原序，与导出包中记录的写出顺序一致"
    )
    warc: str = Field(
        ..., description="导出的自包含 WARC（未压缩）的 Base64 编码；仅含所请"
        "记录及其 revisit 引用链传递依赖，记录字节与原归档一致"
    )


@app.post("/v1/extract", response_model=ExtractResponse, status_code=200)
async def post_extract(payload: ExtractRequest) -> dict[str, Any]:
    return extract(payload.warc, payload.record_ids)


class NavigateRequest(BaseModel):
    warc: str = Field(
        ...,
        description="Base64 编码的 WARC 数据（可带空白/换行），解码后不超过 2 MiB",
    )
    url: str = Field(..., description="起始 HTTP(S) URL（绝对地址）")
    at: str = Field(
        ...,
        description="RFC3339 时点（建议 UTC，如 2021-05-05T10:00:00Z）",
    )


class NavigateHop(BaseModel):
    url: str = Field(..., description="该跳被选取时使用的 URL")
    record_id: str = Field(..., description="选中记录的 WARC-Record-ID 原始值")
    capture_time: str = Field(..., description="选中记录的 WARC-Date")
    http_status: Optional[int] = Field(
        None, description="选中记录自己的 HTTP 状态码；revisit 无 HTTP 头时为 null"
    )


class NavigateResponse(BaseModel):
    path: list[NavigateHop] = Field(..., description="各跳选取结果（含终点）")
    final_url: str = Field(..., description="最终页面 URL")
    body_base64: str = Field(..., description="最终正文 Base64；空正文为空字符串")
    body_source_id: str = Field(
        ..., description="正文来源记录 ID（revisit 为其链终点 response 的 ID）"
    )


@app.post("/v1/navigate", response_model=NavigateResponse, status_code=200)
async def post_navigate(payload: NavigateRequest) -> dict[str, Any]:
    return navigate(payload.warc, payload.url, payload.at)


class SnapshotDiffRequest(BaseModel):
    old_warc: str = Field(
        ...,
        description="旧版 Base64 编码的 WARC（可带空白/换行），解码后不超过 2 MiB",
    )
    new_warc: str = Field(
        ...,
        description="新版 Base64 编码的 WARC（可带空白/换行），解码后不超过 2 MiB",
    )
    at: str = Field(
        ...,
        description="RFC3339 时点（建议 UTC，如 2021-05-05T10:00:00Z）",
    )


class SnapshotSide(BaseModel):
    record_id: str = Field(..., description="该侧选中记录的 WARC-Record-ID 原始值")
    http_status: Optional[int] = Field(
        None, description="该侧选中记录自己的 HTTP 状态码；revisit 无 HTTP 头时为 null"
    )
    body_sha256: str = Field(
        ..., description="该侧还原后正文的 SHA-256（十六进制小写）"
    )


class SnapshotDiffItem(BaseModel):
    url: str = Field(..., description="参与对比的 URL（精确匹配，未规范化）")
    old: Optional[SnapshotSide] = Field(None, description="旧版一侧；缺失为 null")
    new: Optional[SnapshotSide] = Field(None, description="新版一侧；缺失为 null")


class SnapshotDiffSummary(BaseModel):
    only_old: int = Field(..., description="仅旧版可见的 URL 数")
    only_new: int = Field(..., description="仅新版可见的 URL 数")
    changed: int = Field(..., description="内容变化的 URL 数")
    unchanged: int = Field(..., description="未变化的 URL 数")


class SnapshotDiffResponse(BaseModel):
    summary: SnapshotDiffSummary
    only_old: list[SnapshotDiffItem] = Field(
        ..., description="仅旧版可见的 URL，按 URL 字典序"
    )
    only_new: list[SnapshotDiffItem] = Field(
        ..., description="仅新版可见的 URL，按 URL 字典序"
    )
    changed: list[SnapshotDiffItem] = Field(
        ..., description="两侧均可见但内容变化的 URL，按 URL 字典序"
    )
    unchanged: list[SnapshotDiffItem] = Field(
        ..., description="两侧均可见且内容未变化的 URL，按 URL 字典序"
    )


@app.post("/v1/snapshot-diff", response_model=SnapshotDiffResponse,
          status_code=200)
async def post_snapshot_diff(payload: SnapshotDiffRequest) -> dict[str, Any]:
    return snapshot_diff(payload.old_warc, payload.new_warc, payload.at)


class PageAssetsRequest(BaseModel):
    warc: str = Field(
        ...,
        description="Base64 编码的 WARC 数据（可带空白/换行），解码后不超过 2 MiB",
    )
    url: str = Field(..., description="起始 HTTP(S) URL（绝对地址）")
    at: str = Field(
        ...,
        description="RFC3339 时点（建议 UTC，如 2021-05-05T10:00:00Z）",
    )


class PageAssetItem(BaseModel):
    url: str = Field(
        ..., description="资源的绝对 URL（已按 base 解析、去片段、去重）"
    )
    source: str = Field(
        ..., description="来源标签：img / script / link（rel=stylesheet）"
    )
    ok: bool = Field(..., description="终点状态为 2xx 即 true（可用）")
    record_id: Optional[str] = Field(
        None, description="终点记录的 WARC-Record-ID 原始值；未到达终点为 null"
    )
    http_status: Optional[int] = Field(
        None, description="终点记录自己的 HTTP 状态码；未到达终点为 null"
    )
    error: Optional[str] = Field(
        None, description="不可用的原因：capture_not_found / status_not_2xx / "
        "missing_location / invalid_redirect_target / redirect_cycle / "
        "too_many_redirects；可用时为 null"
    )


class PageAssetsResponse(BaseModel):
    final_url: str = Field(
        ..., description="最终页面 URL（根页面重定向跟随后的终点）"
    )
    total: int = Field(..., description="去重后的资源总数")
    usable: int = Field(..., description="可用（终点 2xx）资源数量")
    assets: list[PageAssetItem] = Field(
        ..., description="逐项检查结果，按资源 URL 字典序"
    )


@app.post("/v1/page-assets", response_model=PageAssetsResponse, status_code=200)
async def post_page_assets(payload: PageAssetsRequest) -> dict[str, Any]:
    return page_assets(payload.warc, payload.url, payload.at)


class DigestAuditRequest(BaseModel):
    warc: str = Field(
        ...,
        description="Base64 编码的 WARC 数据（可带空白/换行），解码后不超过 2 MiB；"
        "空字符串表示空 WARC，返回空记录列表",
    )


class DigestAuditRecord(BaseModel):
    order: int = Field(..., description="在归档中的处理序号（从 0 开始）")
    record_id: str = Field(..., description="WARC-Record-ID 原始值")
    block_digest: str = Field(
        ..., description="WARC-Block-Digest 核验结果：absent / match / mismatch"
    )
    payload_digest: str = Field(
        ..., description="WARC-Payload-Digest 核验结果：absent / match / mismatch"
    )


class DigestAuditResponse(BaseModel):
    count: int
    mismatch_count: int = Field(
        ..., description="两项核验结果为 mismatch 的总数（按字段计）"
    )
    records: list[DigestAuditRecord]


@app.post("/v1/digest-audit", response_model=DigestAuditResponse,
          status_code=200)
async def post_digest_audit(payload: DigestAuditRequest) -> dict[str, Any]:
    return digest_audit(payload.warc)


class CaptureIndexRequest(BaseModel):
    warc: str = Field(
        ...,
        description="Base64 编码的 WARC 数据（可带空白/换行），解码后不超过 2 MiB；"
        "空字符串表示空 WARC，返回空抓取列表",
    )
    page_size: int = Field(
        ..., ge=1, le=100, description="每页条数（1～100）"
    )
    cursor: Optional[str] = Field(
        None,
        description="上一页响应中的 next_cursor；缺省从首页开始。cursor 绑定"
        "解压后归档的 SHA-256 与上页末条排序键，跨包或无效 cursor 返回 422；"
        "cursor 与 page_size 无关，翻页间可以改变 page_size",
    )


class CaptureIndexEntry(BaseModel):
    record_id: str = Field(..., description="WARC-Record-ID 原始值")
    record_type: str = Field(..., description="记录类型：response 或 revisit")
    capture_time: str = Field(..., description="抓取时间（WARC-Date 原值）")
    http_status: Optional[int] = Field(
        None, description="该记录自己的 HTTP 状态码；revisit 无 HTTP 头时为 null"
    )
    body_source_id: str = Field(
        ..., description="正文来源记录 ID（response 为自身；revisit 为引用链"
        "终点 response 的 ID）"
    )
    offset: int = Field(
        ..., description="记录在解压后归档中的起始字节偏移（自 WARC 版本行起）"
    )
    length: int = Field(
        ..., description="记录字节长度：含完整记录块、不含记录间分隔符；"
        "data[offset:offset+length] 即原记录字节，切片须能还原原记录"
    )


class CaptureIndexResponse(BaseModel):
    total: int = Field(
        ..., description="参与列出的抓取总条数（与本页无关；缺 URL/日期的记录不计）"
    )
    entries: list[CaptureIndexEntry] = Field(
        ..., description="本页抓取，按（URL、UTC 时刻、归档序号）升序"
    )
    next_cursor: Optional[str] = Field(
        ..., description="下一页 cursor；末页（含归档为空）为 null"
    )


@app.post("/v1/capture-index", response_model=CaptureIndexResponse,
          status_code=200)
async def post_capture_index(payload: CaptureIndexRequest) -> dict[str, Any]:
    return capture_index(payload.warc, payload.page_size, payload.cursor)


class MergeRequest(BaseModel):
    left_warc: str = Field(
        ...,
        description="左包 Base64 编码的 WARC（可带空白/换行），解码后不超过 "
        "2 MiB；空字符串表示空 WARC",
    )
    right_warc: str = Field(
        ...,
        description="右包 Base64 编码的 WARC（可带空白/换行），解码后不超过 "
        "2 MiB；空字符串表示空 WARC",
    )


class MergeRename(BaseModel):
    old_id: str = Field(..., description="右包中被改名的 WARC-Record-ID 原始值")
    new_id: str = Field(..., description="合并包中的新 WARC-Record-ID 值")


class MergeResponse(BaseModel):
    count: int = Field(..., description="合并包中的 response/revisit 记录数")
    merged_warc: str = Field(
        ..., description="合并后的自包含 WARC（未压缩）的 Base64 编码"
    )
    renamed: list[MergeRename] = Field(
        ..., description="右包冲突记录的旧新 ID 对照，按右包原序；无冲突为空列表"
    )


@app.post("/v1/merge", response_model=MergeResponse, status_code=200)
async def post_merge(payload: MergeRequest) -> dict[str, Any]:
    return merge(payload.left_warc, payload.right_warc)


class HttpExchangesRequest(BaseModel):
    warc: str = Field(
        ...,
        description="Base64 编码的 WARC 数据（可带空白/换行），解码后不超过 "
        "2 MiB；空字符串表示空 WARC，返回空配对列表",
    )


class ExchangePair(BaseModel):
    request_id: str = Field(
        ..., description="request 记录的 WARC-Record-ID 原始值"
    )
    response_id: str = Field(
        ..., description="响应侧（response 或 revisit）记录的 WARC-Record-ID "
        "原始值"
    )
    target_uri: Optional[str] = Field(
        None, description="目标 URL，取响应侧记录的 WARC-Target-URI"
    )
    method: str = Field(..., description="HTTP 请求方法（请求行原值）")
    request_target: str = Field(
        ..., description="HTTP 请求目标（请求行原值，如 /index.html?x=1）"
    )
    http_status: Optional[int] = Field(
        None, description="响应状态码；revisit 自身无 HTTP 头时为 null"
    )


class HttpExchangesResponse(BaseModel):
    count: int = Field(..., description="配对成功的 HTTP 交换数")
    pairs: list[ExchangePair] = Field(
        ..., description="配对结果，按 request 记录在归档中的顺序"
    )
    unpaired_requests: list[str] = Field(
        ..., description="未配对的 request 记录 ID（WARC-Record-ID 原始值），"
        "按归档原序"
    )
    unpaired_responses: list[str] = Field(
        ..., description="未配对的 response/revisit 记录 ID（原始值），按归档原序"
    )


@app.post("/v1/http-exchanges", response_model=HttpExchangesResponse,
          status_code=200)
async def post_http_exchanges(payload: HttpExchangesRequest) -> dict[str, Any]:
    return http_exchanges(payload.warc)


class MemberPackRequest(BaseModel):
    warc: str = Field(
        ...,
        description="Base64 编码的 WARC 数据（可带空白/换行），解码后不超过 "
        "2 MiB；空字符串表示空 WARC，返回空包与空 member 列表",
    )


class MemberPackMember(BaseModel):
    record_id: str = Field(..., description="该 member 对应记录的 "
                                            "WARC-Record-ID 原始值")
    offset: int = Field(
        ..., description="该记录 gzip member 在 gzip_warc 中的起始字节偏移"
    )
    length: int = Field(
        ..., description="该记录 gzip member 的压缩字节长度；各 member 范围"
        "连续且互不重叠，gzip_warc[offset:offset+length] 即该记录的独立 "
        "gzip member，单独解压得到完整 WARC 记录及记录分隔符"
    )


class MemberPackResponse(BaseModel):
    count: int = Field(..., description="收录的 response/revisit 记录数")
    gzip_warc: str = Field(
        ..., description="按归档原序逐记录生成的多 member gzip（每记录一个 "
        "独立 gzip member，顺序拼接）的 Base64 编码；gzip 头时间戳固定，"
        "同一输入产出逐字节相同的输出"
    )
    members: list[MemberPackMember] = Field(
        ..., description="各记录 ID 对应的压缩 member 字节范围，按归档原序"
    )


@app.post("/v1/member-pack", response_model=MemberPackResponse,
          status_code=200)
async def post_member_pack(payload: MemberPackRequest) -> dict[str, Any]:
    return member_pack(payload.warc)
