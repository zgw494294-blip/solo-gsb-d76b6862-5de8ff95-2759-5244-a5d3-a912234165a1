"""页面静态资源可还原性检查（POST /v1/page-assets）核心。

给定 Base64 WARC、起始 HTTP(S) URL 与 RFC3339 时点，先以与 /v1/navigate
完全相同的时点选取与重定向跟随还原最终页面，再从其 HTML 正文提取静态
资源引用，逐项检查该时点这些资源能否从同一归档还原：

- 提取 ``img[src]``、``script[src]`` 与 ``rel=stylesheet`` 的
  ``link[href]``（标签与属性名大小写不敏感；``rel`` 按空白分词后包含
  ``stylesheet`` 即算，大小写不敏感，如 ``rel="alternate stylesheet"``）；
- 相对地址按**首个有效** ``<base href>`` 解析：base 的 href 自身可以是
  相对地址（相对最终页 URL 解析），其解析结果必须是 HTTP(S) 绝对地址
  才算有效，否则尝试下一个；没有任何有效 base 时用最终页 URL；
- 解析后的资源地址去掉片段，只保留 HTTP(S) 绝对地址（``data:``、
  ``javascript:``、``mailto:`` 等直接忽略，不计入结果），按绝对 URL
  去重，同一 URL 保留首次出现的来源标签；
- 每个资源独立按同一时点选取抓取并跟随重定向（与 navigate 同一套规则，
  含 8 跳上限与成环检测）；终点记录自己的 HTTP 状态为 2xx 才算可用；
- 无抓取、终点非 2xx、重定向异常（缺 Location、目标非法、成环、超跳数）
  都只在对应条目上标明原因，不使整页失败；根页面的选取/重定向失败与
  归档错误则与 /v1/navigate 一样使整个请求失败（404/422 原样透传）。
"""

from __future__ import annotations

from datetime import datetime
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urldefrag, urljoin, urlsplit

from .navigate import (
    _build_index,
    _follow_redirects,
    _http_url,
    _parse_rfc3339,
)
from .replay import ReplayError, load_records


def _resolve_http_url(raw: Optional[str], base: str) -> Optional[str]:
    """把（可能相对的）URL 字面值按 ``base`` 解析为绝对地址。

    结果是 HTTP(S) 绝对地址时返回去片段后的形式；空值、解析失败或非
    HTTP(S) 一律返回 None（调用方据此跳过该引用或判 base 无效）。
    """
    if not raw or not raw.strip():
        return None
    try:
        resolved = urljoin(base, raw.strip())
        parts = urlsplit(resolved)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    return urldefrag(resolved)[0]


class _AssetHTMLParser(HTMLParser):
    """收集 ``<base href>`` 候选与静态资源引用（原始字面值，暂不解析）。

    HTMLParser 会把标签名与属性名统一成小写；同一属性重复出现时只取
    首次值（与浏览器一致）。``<base>`` 的 href 按文档顺序全部保留，
    由调用方选出首个有效者。自闭合标签经默认的 ``handle_startendtag``
    走 ``handle_starttag``，无需单独处理。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.base_hrefs: list[str] = []
        self.references: list[tuple[str, str]] = []  # (原始 URL 字面值, 来源标签)

    def handle_starttag(self, tag: str,
                        attrs: list[tuple[str, Optional[str]]]) -> None:
        values: dict[str, Optional[str]] = {}
        for name, value in attrs:
            if name not in values:
                values[name] = value

        if tag == "base":
            href = values.get("href")
            if href:
                self.base_hrefs.append(href)
        elif tag == "img":
            self._add(values.get("src"), "img")
        elif tag == "script":
            self._add(values.get("src"), "script")
        elif tag == "link":
            rel_tokens = (values.get("rel") or "").lower().split()
            if "stylesheet" in rel_tokens:
                self._add(values.get("href"), "link")

    def _add(self, raw: Optional[str], source: str) -> None:
        if raw and raw.strip():
            self.references.append((raw, source))


def _check_asset(index, url: str, point_in_time: datetime, at: str) -> dict:
    """检查单个资源：按时点选取并跟随重定向，判定可用性与失败原因。

    无抓取与重定向异常（navigate 的全部 404/422 错误码）只转化为条目
    上的原因标记；到达终点时状态为 2xx 才可用，否则记
    ``status_not_2xx``（终点是 revisit 且自身无 HTTP 头时状态为 None，
    同样不算可用）。
    """
    try:
        _path, _final_url, rec = _follow_redirects(index, url, point_in_time,
                                                   at)
    except ReplayError as exc:
        return {
            "record_id": None,
            "http_status": None,
            "ok": False,
            "error": exc.code,
        }
    status = rec.http_status
    ok = status is not None and 200 <= status < 300
    return {
        "record_id": rec.record_id_raw,
        "http_status": status,
        "ok": ok,
        "error": None if ok else "status_not_2xx",
    }


def page_assets(warc_b64: str, start_url: str, at: str) -> dict:
    """page-assets 入口：返回最终页面静态资源的逐项可还原性结果。"""
    url = _http_url(start_url)
    point_in_time = _parse_rfc3339(at)

    records = load_records(warc_b64, allow_missing_target_uri=True)
    index = _build_index(records)

    # 根页面：与 /v1/navigate 完全相同的选取与重定向跟随；这里的
    # ReplayError（含 404 capture_not_found 与重定向异常）原样向上抛，
    # 响应与原接口一致。
    _path, final_url, page_rec = _follow_redirects(index, url, point_in_time,
                                                   at)

    # 正文按 UTF-8 容错解码；HTML 解析本身对畸形输入宽容，找不到标签即空
    html = page_rec.body.decode("utf-8", errors="replace")
    parser = _AssetHTMLParser()
    parser.feed(html)
    parser.close()

    # 首个有效 <base href>；没有有效 base 时用最终页 URL
    base_url = final_url
    for raw_base in parser.base_hrefs:
        resolved_base = _resolve_http_url(raw_base, final_url)
        if resolved_base is not None:
            base_url = resolved_base
            break

    # 解析相对地址、去片段、只留 HTTP(S)，按绝对 URL 去重（保留首次来源标签）
    sources: dict[str, str] = {}
    for raw, source in parser.references:
        asset_url = _resolve_http_url(raw, base_url)
        if asset_url is not None and asset_url not in sources:
            sources[asset_url] = source

    assets: list[dict] = []
    usable = 0
    for asset_url in sorted(sources):
        result = _check_asset(index, asset_url, point_in_time, at)
        if result["ok"]:
            usable += 1
        assets.append({"url": asset_url, "source": sources[asset_url],
                       **result})

    return {
        "final_url": final_url,
        "total": len(assets),
        "usable": usable,
        "assets": assets,
    }
