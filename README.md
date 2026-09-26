# WARC Revisit Replay API

基于 **Python + FastAPI + [warcio](https://github.com/webrecorder/warcio)** 的 WARC 离线还原服务，提供十个接口：

- `POST /v1/replay`：接收 Base64 编码的自包含 WARC，按 `WARC-Record-ID` 建索引，
  将 `revisit` 记录沿 `WARC-Refers-To` 引用链还原到终点 `response` 的正文；
- `POST /v1/extract`：按 `WARC-Record-ID` 从归档中导出子集——所请记录及其
  revisit 引用链传递依赖，打包为新的自包含 WARC；
- `POST /v1/navigate`：再加上起始 HTTP(S) URL 与 RFC3339 时点，按时点离线还原
  当时浏览器会看到的页面（自动跟随重定向）；
- `POST /v1/snapshot-diff`：接收旧版、新版两份 WARC 与 RFC3339 时点，对比该时点
  两份归档中各自可见的 URL（仅旧版 / 仅新版 / 内容变化 / 未变化）；
- `POST /v1/page-assets`：接收 WARC、起始 URL 与时点，还原最终页面后逐项检查
  其静态资源（img/script/样式表）在该时点能否从同包归档还原；
- `POST /v1/digest-audit`：逐条核验 response/revisit 记录的
  `WARC-Block-Digest` 与 `WARC-Payload-Digest`（sha1/sha256 的无填充
  Base32 摘要），报告每项 absent / match / mismatch 与失配总数；
- `POST /v1/capture-index`：接收 WARC、`page_size`（1～100）与可选 `cursor`，
  按目标 URL 原值、`WARC-Date` 的 UTC 时刻、归档序号升序**分页**列出
  response/revisit 抓取，每项给出记录在**解压后归档**中的 `offset` / `length`
  字节范围；缺 URL 或日期的记录跳过，cursor 绑定解压后归档的 SHA-256；
- `POST /v1/merge`：接收 `left_warc`、`right_warc` 两份 Base64 WARC（各自
  解码 ≤ 2 MiB），把两包的 response/revisit 记录按**左包原序 + 右包原序**
  合成可回放归档；跨包 ID 冲突时只为右包冲突记录生成确定性、全包唯一的
  新 ID 并同步改写其 `WARC-Refers-To`，返回旧新 ID 对照。
- `POST /v1/http-exchanges`：接收 Base64 WARC，按 `request` 与
  `response`/`revisit` **任一侧**的 `WARC-Concurrent-To` 显式关联重建
  HTTP 交换（允许前向引用；URL 或时间相同不构成配对），按请求归档序
  返回双方 ID、目标 URL、请求方法、请求目标与响应状态，未配对的两类
  记录分别按原序列出。
- `POST /v1/member-pack`：接收 Base64 WARC，只收录 `response`/`revisit`，
  按归档原序为**每条记录**生成一个独立 gzip member，拼成可按记录随机定位
  的**多 member gzip**；固定 gzip 时间戳（同输入同字节），返回
  `gzip_warc`（Base64）与各记录 ID 的压缩 `offset`/`length`（连续、互不
  重叠）；末记录缺 `Content-Length` 时补入准确块长，整包解压后交给
  `/v1/replay` 与输入一致，解压后超过 2 MiB 返回 422。

## 一条命令启动（端口 8000）

```bash
docker compose up --build
```

服务启动后监听 **8000** 端口：

- 健康检查：<http://localhost:8000/health>
- 接口文档（Swagger UI）：<http://localhost:8000/docs>

不用容器时的本地等价启动方式：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API

### `GET /health`

```json
{"status": "ok"}
```

### `POST /v1/replay`

请求体：

| 字段   | 类型   | 说明                                                        |
|--------|--------|-------------------------------------------------------------|
| `warc` | string | **Base64 编码**的 WARC；解码后体积 **≤ 2 MiB**，支持 gzip 压缩（整条单流或按记录拼接的多 member gzip 均可）。可含空白/换行，标准与 URL-safe 字母表均接受。 |

响应（`200`）按**归档原序**返回每条 `response` / `revisit` 记录：

| 字段             | 含义                                                                 |
|------------------|----------------------------------------------------------------------|
| `order`          | 在归档中的处理序号（从 0 开始）                                      |
| `record_id`      | `WARC-Record-ID` 原始值                                              |
| `record_type`    | `response` 或 `revisit`                                              |
| `target_uri`     | 目标 URL（`WARC-Target-URI`）                                        |
| `capture_time`   | 抓取时间（`WARC-Date`）                                              |
| `http_status`    | HTTP 状态码（整数）；**revisit 自身无 HTTP 头时为 `null`**           |
| `body_base64`    | 正文的 Base64；**空正文合法**，返回空字符串                          |
| `body_source_id` | 正文来源记录 ID。response 为自身；revisit 为引用链终点 response 的 ID |

revisit 的还原规则：

- 保留**自己的** URL、抓取时间和 HTTP 状态（缺失则为 `null`）；
- **只继承**引用链终点 `response` 的正文；
- `WARC-Refers-To` 允许引用包内任意位置的记录（**允许前向引用、多级引用**）；
- 链的终点必须是 `response`，且该终点带有有效的 HTTP 响应头。
- 其余类型（`warcinfo` / `request` / `metadata` / `resource` 等）会被跳过，不建索引。

#### 示例

构造一个 response 与一个引用它的 revisit：

```bash
curl -s http://localhost:8000/v1/replay \
  -H 'Content-Type: application/json' \
  -d "{\"warc\":\"$(base64 -w0 sample.warc)\"}"
```

响应：

```json
{
  "count": 2,
  "records": [
    {
      "order": 0,
      "record_id": "<urn:uuid:aaaa>",
      "record_type": "response",
      "target_uri": "http://example.com/",
      "capture_time": "2020-01-01T00:00:00Z",
      "http_status": 200,
      "body_base64": "aGVsbG8=",
      "body_source_id": "<urn:uuid:aaaa>"
    },
    {
      "order": 1,
      "record_id": "<urn:uuid:bbbb>",
      "record_type": "revisit",
      "target_uri": "http://example.com/",
      "capture_time": "2021-06-01T00:00:00Z",
      "http_status": null,
      "body_base64": "aGVsbG8=",
      "body_source_id": "<urn:uuid:aaaa>"
    }
  ]
}
```

### `POST /v1/extract`

按 `WARC-Record-ID` 从归档中**导出子集**，打包为新的自包含 WARC。输入归档
完整经过与 `/v1/replay` 相同的解码（2 MiB 上限、gzip）与结构/引用链校验——
归档中任何一处错误（包括未被请求的记录）都使整个请求返回 422。

请求体：

| 字段         | 类型     | 说明 |
|--------------|----------|------|
| `warc`       | string   | **Base64 编码**的 WARC；解码后 **≤ 2 MiB**，支持 gzip（单流或多 member）。 |
| `record_ids` | string[] | 要导出的 `WARC-Record-ID`，**1～100 个**；按与归档内 ID 相同的规则匹配（去空白、去尖括号，裸 URN 与 `<...>` 形式互通），重复自动去重；任一 ID 在归档的 response/revisit 记录中不存在 → **422** `unknown_record_id`。 |

导出规则：

- 导出包**仅含**所请记录，以及从所请 `revisit` 沿 `WARC-Refers-To` 可达的记录
  （中间 revisit 与终点 response）——依赖即使位于所请记录**之后**（前向引用）
  也纳入；其余记录（含 `warcinfo`/`request` 等被跳过的类型）一律丢弃；
- 纳入记录按**归档原序**、以**原始字节**写出：WARC/HTTP 头与负载逐字节保留，
  `Content-Length` 与写出的记录块一致；记录间以 `CRLF CRLF` 分隔，导出包不压缩；
- 因此把导出包交给 `/v1/replay`，所请记录的 HTTP 状态、还原正文与
  `body_source_id` 与原包**完全一致**。

响应（`200`）：

| 字段           | 含义 |
|----------------|------|
| `included_ids` | 实际纳入导出包的记录 ID（`WARC-Record-ID` 原始值），按归档原序，与写出顺序一致 |
| `warc`         | 导出的自包含 WARC（未压缩）的 Base64 编码 |

#### 示例

归档含 response `<urn:uuid:aaaa>`（正文 `hello`）与引用它的 revisit
`<urn:uuid:bbbb>`，只请求 revisit：

```bash
curl -s http://localhost:8000/v1/extract \
  -H 'Content-Type: application/json' \
  -d "{\"warc\":\"$(base64 -w0 sample.warc)\", \
       \"record_ids\":[\"<urn:uuid:bbbb>\"]}"
```

响应（依赖 `<urn:uuid:aaaa>` 被一并纳入，按归档原序）：

```json
{
  "included_ids": ["<urn:uuid:aaaa>", "<urn:uuid:bbbb>"],
  "warc": "V0FSQy8xLjENCldBUkMtVHlwZTogcmVzcG9uc2UNCldBUkMtUmVjb3JkLUlEOiA8dXJuOnV1aWQ6YWFhYT4NCldBUkMtRGF0ZTogMjAyMC0wMS0wMVQwMDowMDowMFoNCldBUkMtVGFyZ2V0LVVSSTogaHR0cDovL2V4YW1wbGUuY29tLw0KQ29udGVudC1UeXBlOiBhcHBsaWNhdGlvbi9odHRwOyBtc2d0eXBlPXJlc3BvbnNlDQpDb250ZW50LUxlbmd0aDogMjQNCg0KSFRUUC8xLjEgMjAwIE9LDQoNCmhlbGxvDQoNCldBUkMvMS4xDQpXQVJDLVR5cGU6IHJldmlzaXQNCldBUkMtUmVjb3JkLUlEOiA8dXJuOnV1aWQ6YmJiYj4NCldBUkMtRGF0ZTogMjAyMS0wNi0wMVQwMDowMDowMFoNCldBUkMtVGFyZ2V0LVVSSTogaHR0cDovL2V4YW1wbGUuY29tLw0KV0FSQy1SZWZlcnMtVG86IDx1cm46dXVpZDphYWFhPg0KQ29udGVudC1MZW5ndGg6IDANCg0K"
}
```

把该 `warc` 解码后再提交给 `/v1/replay`，可还原出与原归档相同的两条记录
（revisit `<urn:uuid:bbbb>` 的正文来自 `<urn:uuid:aaaa>`）。

### `POST /v1/navigate`

在 `/v1/replay` 的同一套 WARC 校验之上，按**起始 URL 与时点**离线还原页面。

请求体：

| 字段   | 类型   | 说明 |
|--------|--------|------|
| `warc` | string | **Base64 编码**的 WARC；解码后 **≤ 2 MiB**，支持 gzip（单流或多 member）。 |
| `url`  | string | 起始 **HTTP(S)** 绝对 URL（`http://`/`https://`）。 |
| `at`   | string | **RFC3339** 时点，建议 UTC（如 `2021-05-05T10:00:00Z`）；也接受 `±HH:MM` 偏移，内部换算到 UTC。 |

选取与跟随规则：

- 对每个 URL，选择 `WARC-Date` **不晚于** `at` 的最新 `response`/`revisit`；
  同一 URL、同一 `WARC-Date` 有多条时，取归档中**靠后**的一条；
- 缺 `WARC-Target-URI` 或 `WARC-Date` 的记录**不参与选择**，但不会连累整包：
  即使同包含有缺 URL 的记录，其余 URL 的有效抓取仍正常返回；`WARC-Date`
  非法（非 RFC3339）整个请求返回 **422**；
- 选中 **301/302/303/307/308** 时，取**该记录自己**的 HTTP `Location`，按当前
  URL 解析相对地址，继续按时点选取（最多 **8 跳**）；
- 缺 `Location`、Location 解析出的目标非 HTTP(S)、重定向成环、超过 8 跳 → **422**；
- 链上任一 URL 在时点前没有合格抓取 → **404**；
- **revisit 只继承链终点 response 的正文**，不借用终点的状态码或 Location
  （即 revisit 自己带 3xx + Location 才会被当作重定向）。

响应（`200`）：

| 字段             | 含义 |
|------------------|------|
| `path`           | 各跳列表，顺序即跟随顺序，**含终点**；每项见下 |
| `path[].url`     | 该跳被选取时使用的 URL（已按 RFC3986 解析相对地址、去除 fragment） |
| `path[].record_id` | 选中记录的 `WARC-Record-ID` 原始值 |
| `path[].capture_time` | 选中记录的 `WARC-Date` |
| `path[].http_status` | 选中记录**自己**的 HTTP 状态码；revisit 无 HTTP 头时为 `null` |
| `final_url`      | 最终页面 URL（终点那一跳的 URL） |
| `body_base64`    | 最终正文 Base64（revisit 已还原为链终点 response 的正文） |
| `body_source_id` | 正文来源记录 ID（终点为 revisit 时为其链终点 response 的 ID） |

#### 示例

```bash
curl -s http://localhost:8000/v1/navigate \
  -H 'Content-Type: application/json' \
  -d "{\"warc\":\"$(base64 -w0 sample.warc)\", \
       \"url\":\"http://example.com/\", \
       \"at\":\"2021-06-01T00:00:00Z\"}"
```

假设 `http://example.com/` 在 2021-01-01 有一次 301 到 `/home`，后者在
2021-02-01 返回 200 正文：

```json
{
  "path": [
    {
      "url": "http://example.com/",
      "record_id": "<urn:uuid:aaaa>",
      "capture_time": "2021-01-01T00:00:00Z",
      "http_status": 301
    },
    {
      "url": "http://example.com/home",
      "record_id": "<urn:uuid:bbbb>",
      "capture_time": "2021-02-01T00:00:00Z",
      "http_status": 200
    }
  ],
  "final_url": "http://example.com/home",
  "body_base64": "aG9tZQ==",
  "body_source_id": "<urn:uuid:bbbb>"
}
```

时点更早（如 `2020-06-01T00:00:00Z`）则不会看到这次重定向，直接返回该 URL
当时最新的抓取。

### `POST /v1/snapshot-diff`

对比**旧版、新版两份归档**在同一时点各自可见的 URL。

请求体：

| 字段       | 类型   | 说明 |
|------------|--------|------|
| `old_warc` | string | 旧版 **Base64 编码**的 WARC；解码后 **≤ 2 MiB**，支持 gzip（单流或多 member）。 |
| `new_warc` | string | 新版 **Base64 编码**的 WARC；限制同上。 |
| `at`       | string | **RFC3339** 时点，建议 UTC（如 `2021-05-05T10:00:00Z`）；也接受 `±HH:MM` 偏移，内部换算到 UTC。 |

快照与对比规则：

- 每份归档**独立**经过与 `/v1/replay` 相同的结构校验与 revisit 正文还原；
  `WARC-Record-ID` 重复**只在同一份归档内**判错，两份归档出现相同 ID 互不影响；
- 每份归档以 URL 建立快照：只纳入 `WARC-Date` **不晚于** `at` 的
  `response`/`revisit`，取（抓取时间，归档序号）**最大**的一条（与
  `/v1/navigate` 的选取规则一致）；缺 `WARC-Target-URI` 或 `WARC-Date` 的记录
  **不入快照**（不连累整包），`WARC-Date` 非法（非 RFC3339）整个请求返回 **422**；
- 按 **URL 字典序**列出四类：仅旧版（`only_old`）、仅新版（`only_new`）、
  内容变化（`changed`）、未变化（`unchanged`）；
- **内容变化**只看还原后的正文字节与记录**自身**的 HTTP 状态是否不同；
  记录 ID、抓取时间与正文来源（`body_source_id`）的差异都不算内容变化。

响应（`200`）：

| 字段                          | 含义 |
|-------------------------------|------|
| `summary`                     | 四类 URL 的数量汇总：`only_old` / `only_new` / `changed` / `unchanged` |
| `only_old` / `only_new` / `changed` / `unchanged` | 四类 URL 列表，各自按 URL 字典序；每项见下 |
| `…[].url`                     | 参与对比的 URL（精确匹配，未规范化） |
| `…[].old` / `…[].new`         | 该侧选中记录的信息；该侧无此 URL 时为 `null` |
| `…[].old.record_id`           | 选中记录的 `WARC-Record-ID` 原始值 |
| `…[].old.http_status`         | 选中记录**自己**的 HTTP 状态码；revisit 无 HTTP 头时为 `null` |
| `…[].old.body_sha256`         | 还原后正文的 SHA-256（十六进制小写；revisit 为链终点 response 的正文） |

#### 示例

```bash
curl -s http://localhost:8000/v1/snapshot-diff \
  -H 'Content-Type: application/json' \
  -d "{\"old_warc\":\"$(base64 -w0 old.warc)\", \
       \"new_warc\":\"$(base64 -w0 new.warc)\", \
       \"at\":\"2021-06-01T00:00:00Z\"}"
```

假设旧版中 `http://example.com/` 的正文为 `hello-v1`、`http://example.com/old`
仅旧版可见；新版中 `http://example.com/` 的正文变为 `hello-v2`、
`http://example.com/new` 仅新版可见：

```json
{
  "summary": {"only_old": 1, "only_new": 1, "changed": 1, "unchanged": 0},
  "only_old": [
    {
      "url": "http://example.com/old",
      "old": {
        "record_id": "<urn:uuid:o2>",
        "http_status": 200,
        "body_sha256": "44866519ecce15be809e246a36309fd6415625f17b887544c9127d4f491dac15"
      },
      "new": null
    }
  ],
  "only_new": [
    {
      "url": "http://example.com/new",
      "old": null,
      "new": {
        "record_id": "<urn:uuid:n2>",
        "http_status": 200,
        "body_sha256": "b1b8c6784e98fb26dff5ddd41bb132867b50314f810acdd1c05164c831a41866"
      }
    }
  ],
  "changed": [
    {
      "url": "http://example.com/",
      "old": {
        "record_id": "<urn:uuid:o1>",
        "http_status": 200,
        "body_sha256": "b89bec53ab1fcbaf824090a0d619c01dd25c128b40613da016ccb370eecb5f5a"
      },
      "new": {
        "record_id": "<urn:uuid:n1>",
        "http_status": 200,
        "body_sha256": "028e5ba875b80a77cd1b68ed3730be9429f13ee265fbf1e5018a6d9a6b5a759d"
      }
    }
  ],
  "unchanged": []
}
```

### `POST /v1/page-assets`

在 `/v1/navigate` 的时点选取与重定向跟随之上，检查最终页面的**静态资源**
在该时点能否从同一归档还原。

请求体（与 `/v1/navigate` 相同）：

| 字段   | 类型   | 说明 |
|--------|--------|------|
| `warc` | string | **Base64 编码**的 WARC；解码后 **≤ 2 MiB**，支持 gzip（单流或多 member）。 |
| `url`  | string | 起始 **HTTP(S)** 绝对 URL。 |
| `at`   | string | **RFC3339** 时点，建议 UTC（如 `2021-05-05T10:00:00Z`）。 |

处理规则：

- 先用与 `/v1/navigate` **完全相同**的规则还原最终页面（时点选取 + 跟随
  重定向）；根页面无抓取（**404**）、重定向异常或归档错误（**422**）时，
  响应与原接口一致，不进入资源检查；
- 从最终页面 HTML 正文提取 `img[src]`、`script[src]` 与 `rel=stylesheet`
  的 `link[href]`（标签/属性名大小写不敏感；`rel` 按空白分词后包含
  `stylesheet` 即算）；
- 相对地址按**首个有效** `<base href>` 解析（base 的 href 自身可为相对
  地址，相对最终页 URL 解析；其解析结果必须是 HTTP(S) 绝对地址才有效，
  否则尝试下一个；没有有效 base 时用最终页 URL），去掉片段后只保留
  HTTP(S) 资源（`data:`、`javascript:` 等直接忽略），按绝对 URL 去重，
  同一 URL 保留首次出现的来源标签；
- 每个资源**独立**按同一时点选取抓取并跟随重定向（同样最多 8 跳）；
  终点记录自己的 HTTP 状态为 **2xx** 才算可用；
- 无抓取、终点非 2xx、重定向异常都只在对应条目上标明原因（`error` 字段），
  **不使整页失败**。

响应（`200`）：

| 字段                  | 含义 |
|-----------------------|------|
| `final_url`           | 最终页面 URL（根页面重定向跟随后的终点） |
| `total`               | 去重后的资源总数 |
| `usable`              | 可用（终点 2xx）资源数量 |
| `assets`              | 逐项结果，按资源 URL 字典序；每项见下 |
| `assets[].url`        | 资源的绝对 URL（已按 base 解析、去片段、去重） |
| `assets[].source`     | 来源标签：`img` / `script` / `link`（rel=stylesheet） |
| `assets[].ok`         | 终点状态为 2xx 即 `true` |
| `assets[].record_id`  | 终点记录的 `WARC-Record-ID` 原始值；未到达终点为 `null` |
| `assets[].http_status`| 终点记录**自己**的 HTTP 状态码；未到达终点为 `null` |
| `assets[].error`      | 不可用原因（见下）；可用时为 `null` |

`assets[].error` 取值：`capture_not_found`（时点前无抓取）、
`status_not_2xx`（终点状态非 2xx，含 revisit 自身无 HTTP 头状态为
`null` 的情形）、`missing_location` / `invalid_redirect_target` /
`redirect_cycle` / `too_many_redirects`（重定向异常，含义与
`/v1/navigate` 的同名错误一致）。

#### 示例

```bash
curl -s http://localhost:8000/v1/page-assets \
  -H 'Content-Type: application/json' \
  -d "{\"warc\":\"$(base64 -w0 sample.warc)\", \
       \"url\":\"http://example.com/\", \
       \"at\":\"2021-06-01T00:00:00Z\"}"
```

假设最终页面 HTML 含 `<img src="/logo.png">`、
`<script src="app.js"></script>` 与
`<link rel="stylesheet" href="/style.css">`，其中 `logo.png` 与
`style.css` 在该时点有 200 抓取，`app.js` 没有合格抓取：

```json
{
  "final_url": "http://example.com/",
  "total": 3,
  "usable": 2,
  "assets": [
    {
      "url": "http://example.com/app.js",
      "source": "script",
      "ok": false,
      "record_id": null,
      "http_status": null,
      "error": "capture_not_found"
    },
    {
      "url": "http://example.com/logo.png",
      "source": "img",
      "ok": true,
      "record_id": "<urn:uuid:p1>",
      "http_status": 200,
      "error": null
    },
    {
      "url": "http://example.com/style.css",
      "source": "link",
      "ok": true,
      "record_id": "<urn:uuid:c1>",
      "http_status": 200,
      "error": null
    }
  ]
}
```

### `POST /v1/digest-audit`

逐条核验归档中 `response` / `revisit` 记录的 `WARC-Block-Digest` 与
`WARC-Payload-Digest`。输入归档完整经过与 `/v1/replay` 相同的解码
（2 MiB 上限、gzip）与结构/引用链校验——归档中任何一处结构错误都使整个
请求返回 422。

请求体：

| 字段   | 类型   | 说明 |
|--------|--------|------|
| `warc` | string | **Base64 编码**的 WARC；解码后 **≤ 2 MiB**，支持 gzip（单流或多 member）。 |

摘要格式与核验口径：

- 摘要值形如 **`<算法>:<Base32>`**（如
  `sha1:EIXNIFVWRF6HI2HGZIS7J43FJMLM4H6K`）；支持 **sha1**、**sha256**，
  Base32 为**无填充**形式、**大小写不限**（算法名同样大小写不敏感）；
- **Block 摘要**对**原始记录块**计算：**含 HTTP 头、不含 WARC 头**；缺
  `Content-Length` 的末记录，记录块**以 EOF 为界**；
- **Payload 摘要**对 **HTTP 正文**计算；`revisit` 用**引用链终点
  response 的正文**（与 `/v1/replay` 的还原口径一致），Block 摘要仍按
  revisit 自己的记录块计算；
- 摘要**格式错误**（缺算法前缀、Base32 字符非法、长度与算法不符）或
  **算法不受支持** → **422**，指出记录 ID 与字段（见错误处理一节）；
- 两个字段各自标为 `absent`（未声明）/ `match` / `mismatch`；**失配不
  阻断后续记录**，`warcinfo` 等其余类型不参与核验。

响应（`200`）按**归档原序**返回每条 response/revisit 记录：

| 字段                      | 含义 |
|---------------------------|------|
| `count`                   | 参与核验的记录数 |
| `mismatch_count`          | 两项核验结果为 `mismatch` 的总数（按字段计，一条记录最多计 2） |
| `records`                 | 逐条结果，按归档原序 |
| `records[].order`         | 在归档中的处理序号（从 0 开始） |
| `records[].record_id`     | `WARC-Record-ID` 原始值 |
| `records[].block_digest`  | `WARC-Block-Digest` 核验结果：`absent` / `match` / `mismatch` |
| `records[].payload_digest`| `WARC-Payload-Digest` 核验结果：`absent` / `match` / `mismatch` |

#### 示例

先用 Python 造一份带正确摘要的样例归档（response 正文 `hello`，加一个
引用它的 revisit；revisit 的 Block 摘要故意写错以演示 `mismatch`）：

```bash
python3 - <<'EOF' > sample.warc
import base64, hashlib, sys

def b32(data, algo="sha1"):
    d = hashlib.new(algo, data).digest()
    return base64.b32encode(d).decode().rstrip("=")

body = b"hello"
http = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n" + body
resp = (
    b"WARC/1.1\r\nWARC-Type: response\r\nWARC-Record-ID: <urn:uuid:aaaa>\r\n"
    b"WARC-Date: 2020-01-01T00:00:00Z\r\nWARC-Target-URI: http://example.com/\r\n"
    b"Content-Type: application/http; msgtype=response\r\n"
    + f"WARC-Block-Digest: sha1:{b32(http)}\r\n".encode()
    + f"WARC-Payload-Digest: sha1:{b32(body)}\r\n".encode()
    + f"Content-Length: {len(http)}\r\n\r\n".encode() + http + b"\r\n\r\n"
)
revisit = (
    b"WARC/1.1\r\nWARC-Type: revisit\r\nWARC-Record-ID: <urn:uuid:bbbb>\r\n"
    b"WARC-Date: 2021-06-01T00:00:00Z\r\nWARC-Target-URI: http://example.com/\r\n"
    b"WARC-Refers-To: <urn:uuid:aaaa>\r\n"
    # revisit 的 Payload 摘要按链终点 response 的正文计算
    + f"WARC-Payload-Digest: sha256:{b32(body, 'sha256')}\r\n".encode()
    + b"WARC-Block-Digest: sha1:US2IVAONVMPBUXOTPED5NSC4UHDB3XD4\r\n"  # 故意写错
    + b"Content-Length: 0\r\n\r\n\r\n\r\n"
)
sys.stdout.buffer.write(resp + revisit)
EOF

curl -s http://localhost:8000/v1/digest-audit \
  -H 'Content-Type: application/json' \
  -d "{\"warc\":\"$(base64 -w0 sample.warc)\"}"
```

响应（revisit 的 Payload 摘要命中链终点 response 的正文，Block 摘要失配
不阻断、计入 `mismatch_count`）：

```json
{
  "count": 2,
  "mismatch_count": 1,
  "records": [
    {
      "order": 0,
      "record_id": "<urn:uuid:aaaa>",
      "block_digest": "match",
      "payload_digest": "match"
    },
    {
      "order": 1,
      "record_id": "<urn:uuid:bbbb>",
      "block_digest": "mismatch",
      "payload_digest": "match"
    }
  ]
}
```

### `POST /v1/capture-index`

按（**目标 URL 原值**、`WARC-Date` 的 **UTC 时刻**、**归档序号**）升序
**分页**列出归档中的 `response` / `revisit` 抓取。输入归档完整经过与
`/v1/replay` 相同的解码（2 MiB 上限、gzip）与结构/引用链校验——任何一处
错误（包括未被列出的记录）都使整个请求返回 422。

请求体：

| 字段        | 类型     | 说明 |
|-------------|----------|------|
| `warc`      | string   | **Base64 编码**的 WARC；解码后 **≤ 2 MiB**，支持 gzip（单流或多 member）。空字符串表示空 WARC，返回空抓取列表。 |
| `page_size` | integer  | **每页条数，1～100**（必填，超出范围或类型不符 → 422）。 |
| `cursor`    | string   | 上一页响应中的 `next_cursor`；**可选**，缺省从首页开始。 |

列出与排序规则：

- 排序键为 **（URL 原值、UTC 时刻、归档序号）**：URL 按 `WARC-Target-URI`
  原值做码点序比较，**不做任何规范化**（大小写、结尾斜杠不同即不同 URL）；
  `WARC-Date` 接受 `Z` / `±HH:MM` 偏移并统一换算到 **UTC** 比较，同一 URL、
  同一时刻的记录**全部保留**（不去重），按归档序号（含被跳过类型的真实序号）
  升序；
- 缺 `WARC-Target-URI` **或** `WARC-Date` 的记录**跳过**（不连累整包，与
  `/v1/navigate` 一致）；两者俱在但日期非法（非 RFC3339）→ **422**
  `invalid_capture_date`；`warcinfo` / `request` 等其余类型不列出；
- `http_status` 是记录**自己**的状态码（revisit 无 HTTP 头时为 `null`）；
  `body_source_id` 为正文来源记录 ID（response 为自身，revisit 为引用链终点
  response 的 ID，口径与 `/v1/replay` 一致）；
- `offset` / `length` 是记录在**解压后**归档字节流中的范围
  `[offset, offset+length)`：自 **WARC 版本行**起、含完整 WARC/HTTP 头与
  记录块、**不含记录间分隔符**（`CRLF CRLF`）；对该归档做
  `data[offset:offset+length]` 切片即原记录字节，可直接重新解析；
  缺 `Content-Length` 的末记录范围延伸至 EOF。

分页与 `cursor`：

- 响应固定为 `total`（参与列出的抓取总数，与本页无关）、`entries`（本页）、
  `next_cursor`；还有后续记录时 `next_cursor` 为不透明字符串，**末页为
  `null`**（空归档、以及 cursor 已指向末条记录时也是 `null`，后者返回空页）；
- `cursor` 内部绑定**解压后归档的 SHA-256** 与上页末条的排序键。把它带到
  **另一份归档**（即使内容只差一个字节）、内容损坏或它指向的排序键在归档中
  不存在 → **422** `invalid_cursor`；cursor 与 `page_size` **无关**，翻页时
  可以改变每页条数。

响应（`200`）：

| 字段                       | 含义 |
|----------------------------|------|
| `total`                    | 参与列出的抓取总条数（缺 URL/日期的记录不计） |
| `entries`                  | 本页抓取，按（URL、UTC 时刻、归档序号）升序 |
| `entries[].record_id`      | `WARC-Record-ID` 原始值 |
| `entries[].record_type`    | `response` 或 `revisit` |
| `entries[].capture_time`   | 抓取时间（`WARC-Date` 原值） |
| `entries[].http_status`    | 记录自己的 HTTP 状态码；revisit 无 HTTP 头时为 `null` |
| `entries[].body_source_id` | 正文来源记录 ID（revisit 为引用链终点 response 的 ID） |
| `entries[].offset`         | 记录在解压后归档中的起始字节偏移（自 WARC 版本行起） |
| `entries[].length`         | 记录字节长度（含完整记录块、不含记录间分隔符） |
| `next_cursor`              | 下一页 cursor；末页为 `null` |

#### 示例

先造一份含两条 response 与一个 revisit 的样例归档（revisit
`<urn:uuid:rev>` 引用 `<urn:uuid:a>`）：

```bash
python3 - <<'EOF' > sample-index.warc
import sys

def rec(rid, rtype="response", block=b"", extra=b"",
        date="2020-01-01T00:00:00Z", uri="http://example.com/"):
    return (
        f"WARC/1.1\r\nWARC-Type: {rtype}\r\nWARC-Record-ID: {rid}\r\n"
        f"WARC-Date: {date}\r\nWARC-Target-URI: {uri}\r\n"
        f"Content-Length: {len(block)}\r\n".encode()
        + extra + b"\r\n" + block + b"\r\n\r\n"
    )

def http(line="HTTP/1.1 200 OK", body=b""):
    return line.encode() + b"\r\n\r\n" + body

out = rec("<urn:uuid:a>", block=http(body=b"hello-v1"))
out += rec("<urn:uuid:b>", uri="http://example.com/news",
           block=http("HTTP/1.1 404 Not Found", b"nf"))
out += rec("<urn:uuid:rev>", rtype="revisit", block=b"",
           date="2021-06-01T00:00:00Z",
           extra=b"WARC-Refers-To: <urn:uuid:a>\r\n")
sys.stdout.buffer.write(out)
EOF
```

第一页（`page_size=2`；`http://example.com/` 的 2020 response 与 2021 revisit
排在 `http://example.com/news` 之前）：

```bash
curl -s http://localhost:8000/v1/capture-index \
  -H 'Content-Type: application/json' \
  -d "{\"warc\":\"$(base64 -w0 sample-index.warc)\", \"page_size\":2}"
```

```json
{
  "total": 3,
  "entries": [
    {
      "record_id": "<urn:uuid:a>",
      "record_type": "response",
      "capture_time": "2020-01-01T00:00:00Z",
      "http_status": 200,
      "body_source_id": "<urn:uuid:a>",
      "offset": 0,
      "length": 181
    },
    {
      "record_id": "<urn:uuid:rev>",
      "record_type": "revisit",
      "capture_time": "2021-06-01T00:00:00Z",
      "http_status": null,
      "body_source_id": "<urn:uuid:a>",
      "offset": 375,
      "length": 184
    }
  ],
  "next_cursor": "eyJ2IjoxLCJzaGEiOiI1NDQ3NDc1NTk2Y2ZlNjJmOGFlNjE1MDkwNDVhZTJiNDYzMDdlY2M2NzRmZTcyODcwNjc4YTY2M2JlMzVlYTQ0IiwidXJsIjoiaHR0cDovL2V4YW1wbGUuY29tLyIsInRzIjoiMjAyMS0wNi0wMVQwMDowMDowMFoiLCJvcmRlciI6Mn0"
}
```

带上 `cursor` 取第二页（末页，`next_cursor` 为 `null`）：

```bash
curl -s http://localhost:8000/v1/capture-index \
  -H 'Content-Type: application/json' \
  -d "{\"warc\":\"$(base64 -w0 sample-index.warc)\", \"page_size\":2, \
       \"cursor\":\"eyJ2IjoxLCJzaGEiOiI1NDQ3NDc1NTk2Y2ZlNjJmOGFlNjE1MDkwNDVhZTJiNDYzMDdlY2M2NzRmZTcyODcwNjc4YTY2M2JlMzVlYTQ0IiwidXJsIjoiaHR0cDovL2V4YW1wbGUuY29tLyIsInRzIjoiMjAyMS0wNi0wMVQwMDowMDowMFoiLCJvcmRlciI6Mn0\"}"
```

```json
{
  "total": 3,
  "entries": [
    {
      "record_id": "<urn:uuid:b>",
      "record_type": "response",
      "capture_time": "2020-01-01T00:00:00Z",
      "http_status": 404,
      "body_source_id": "<urn:uuid:b>",
      "offset": 185,
      "length": 186
    }
  ],
  "next_cursor": null
}
```

用 `offset` / `length` 即可从解压后的归档中切出任意一条原始记录
（gzip 输入同样相对解压后字节流）：

```bash
python3 - <<'EOF'
import base64, json, urllib.request

warc_b64 = base64.b64encode(open("sample-index.warc", "rb").read()).decode()
req = urllib.request.Request(
    "http://localhost:8000/v1/capture-index",
    data=json.dumps({"warc": warc_b64, "page_size": 3}).encode(),
    headers={"Content-Type": "application/json"})
page = json.load(urllib.request.urlopen(req))
data = base64.b64decode(warc_b64)
for e in page["entries"]:
    chunk = data[e["offset"]:e["offset"] + e["length"]]
    assert chunk.startswith(b"WARC/")          # 自版本行起
    name = e["record_id"].strip("<>").replace(":", "_") + ".warc"
    open(name, "wb").write(chunk)
print("切出", page["total"], "条记录，均以 WARC 版本行起、不含分隔符")
EOF
```

### `POST /v1/merge`

接收**左、右两份** Base64 编码的 WARC，把两包的 `response`/`revisit` 记录按
**左包原序 + 右包原序**合成一份可回放归档。两份归档各自完整经过与
`/v1/replay` 相同的解码（2 MiB 上限、gzip）与结构/引用链校验（**先左后右**）
——任一包有任何错误（包括未参与合并的记录）都使整个请求返回 422。

请求体：

| 字段         | 类型   | 说明 |
|--------------|--------|------|
| `left_warc`  | string | 左包 **Base64 编码**的 WARC；解码后 **≤ 2 MiB**，支持 gzip（单流或多 member）。空字符串表示空 WARC。 |
| `right_warc` | string | 右包 **Base64 编码**的 WARC；限制同上。 |

合并规则：

- 合并包**只含**两包的 `response`/`revisit` 记录（`warcinfo`/`request` 等
  类型不纳入），按左包原序、右包原序排列，以**原始字节**写出：除下列
  改名与补长之外，WARC/HTTP 头与负载逐字节保留，记录间以 `CRLF CRLF`
  分隔，合并包不压缩；
- **跨包 ID 冲突**：按与归档内 ID 相同的规范化规则（去空白、去尖括号）
  比较，右包记录的 ID 已存在于左包时，**只为右包冲突记录**生成确定性、
  全包唯一的新 ID（`urn:uuid:` 形式的 UUIDv5——同一旧 ID 永远映射到同一
  新 ID，重复请求结果一致），并**同步改写**右包内部指向它的
  `WARC-Refers-To`；左包记录与其余右包记录的字节保持不变，左包内部的
  引用链不受影响；
- 改名按**完整字段值**进行：WARC 头字段值允许折叠在以空格/制表符开头的
  续行上（解析时续行拼入字段值）。若被改名的 `WARC-Record-ID` /
  `WARC-Refers-To` 带有续行，整个字段（首行连同全部续行）会收敛为单行
  新值，**续行中残留的旧值一并清除**（新行沿用该字段首行的换行符，
  兼容 CRLF、LF）；其他 WARC/HTTP 头（包括它们各自的续行）与负载字节
  一律原样保留，无冲突的记录不做任何改写；
- 右包 revisit 引用链所指向的记录必须本来就在右包内（各包独立校验，
  不允许借左包记录满足引用），改名后右包内部引用链保持完整；
- **左包末记录缺 `Content-Length`**（其记录块按规则延伸至左包 EOF）且其后
  还有右包记录时，为该记录注入 `Content-Length`（取实际块长），保证拼接后
  块长度与边界正确、右包记录不会被吞入左包末记录的块中；右包为空时不注入；
- 合并包（未压缩）超过 **2 MiB** → **422** `payload_too_large`。

响应（`200`）：

| 字段                | 含义 |
|---------------------|------|
| `count`             | 合并包中的 response/revisit 记录数 |
| `merged_warc`       | 合并后的自包含 WARC（未压缩）的 Base64 编码 |
| `renamed`           | 右包冲突记录的旧新 ID 对照，按右包原序；无冲突为空列表 |
| `renamed[].old_id`  | 右包中被改名的 `WARC-Record-ID` 原始值 |
| `renamed[].new_id`  | 合并包中的新 `WARC-Record-ID` 值 |

把 `merged_warc` 交给 `/v1/replay`，记录顺序、HTTP 状态、还原正文与原包
一致，revisit 的正文来源 ID 随改名映射替换。交付前合并包会再经一次完整
的回放口径校验（结构、引用链、记录数、顺序与 ID 集合）；改名或拼接一旦
无法形成可回放归档，接口返回 **422** `invalid_merged_warc` 而不是 200 +
无法回放的 `merged_warc`。

#### 示例

左包含 response `<urn:uuid:a>`（正文 `hello-left`）；右包含**同名** response
`<urn:uuid:a>`（正文 `hello-right`）与引用它的 revisit `<urn:uuid:v>`：

```bash
curl -s http://localhost:8000/v1/merge \
  -H 'Content-Type: application/json' \
  -d "{\"left_warc\":\"$(base64 -w0 /tmp/left.warc)\", \
       \"right_warc\":\"$(base64 -w0 /tmp/right.warc)\"}"
```

响应（右包冲突的 `<urn:uuid:a>` 得到确定性新 ID，`<urn:uuid:v>` 的
`WARC-Refers-To` 已同步改写）：

```json
{
  "count": 3,
  "merged_warc": "V0FSQy8xLjENCldBUkMtVHlwZTogcmVzcG9uc2UNCldBUkMtUmVjb3JkLUlEOiA8dXJuOnV1aWQ6YT4NCldBUkMtRGF0ZTogMjAyMC0wMS0wMVQwMDowMDowMFoNCldBUkMtVGFyZ2V0LVVSSTogaHR0cDovL2V4YW1wbGUuY29tLw0KQ29udGVudC1MZW5ndGg6IDI5DQoNCkhUVFAvMS4xIDIwMCBPSw0KDQpoZWxsby1sZWZ0DQoNCldBUkMvMS4xDQpXQVJDLVR5cGU6IHJlc3BvbnNlDQpXQVJDLVJlY29yZC1JRDogPHVybjp1dWlkOjA1ZTMzNjI3LTBiYzktNTNjYS04YjM0LTdiMjIxZjkwMTMyZj4NCldBUkMtRGF0ZTogMjAyMC0wMS0wMVQwMDowMDowMFoNCldBUkMtVGFyZ2V0LVVSSTogaHR0cDovL2V4YW1wbGUuY29tL3BhZ2UNCkNvbnRlbnQtTGVuZ3RoOiAzMA0KDQpIVFRQLzEuMSAyMDAgT0sNCg0KaGVsbG8tcmlnaHQNCg0KV0FSQy8xLjENCldBUkMtVHlwZTogcmV2aXNpdA0KV0FSQy1SZWNvcmQtSUQ6IDx1cm46dXVpZDp2Pg0KV0FSQy1EYXRlOiAyMDIxLTA2LTAxVDAwOjAwOjAwWg0KV0FSQy1UYXJnZXQtVVJJOiBodHRwOi8vZXhhbXBsZS5jb20vcGFnZQ0KQ29udGVudC1MZW5ndGg6IDANCldBUkMtUmVmZXJzLVRvOiA8dXJuOnV1aWQ6MDVlMzM2MjctMGJjOS01M2NhLThiMzQtN2IyMjFmOTAxMzJmPg0KDQo=",
  "renamed": [
    {
      "old_id": "<urn:uuid:a>",
      "new_id": "<urn:uuid:05e33627-0bc9-53ca-8b34-7b221f90132f>"
    }
  ]
}
```

再把 `merged_warc` 交给 `/v1/replay`：左包 `<urn:uuid:a>` 仍还原 `hello-left`；
右包 `<urn:uuid:v>` 的正文为 `hello-right`，且 `body_source_id` 已随映射变为
`<urn:uuid:05e33627-0bc9-53ca-8b34-7b221f90132f>`。

### `POST /v1/http-exchanges`

按**显式关联**重建 `request` 与 `response`/`revisit` 的 HTTP 交换。输入
归档完整经过与 `/v1/replay` 相同的解码（2 MiB 上限、gzip）与结构/引用链
校验；此外 `request` 记录一并解析：与 response/revisit 一样必须带
`WARC-Record-ID`（缺失 → `missing_record_id`），ID 唯一性检查覆盖
request/response/revisit **全集**（重复 → `duplicate_record_id`），且
request 记录必须是合法的 HTTP 请求（否则 →
`request_without_http_headers`，与 response 的同类校验对应）。

请求体：

| 字段   | 类型   | 说明 |
|--------|--------|------|
| `warc` | string | **Base64 编码**的 WARC；解码后 **≤ 2 MiB**，支持 gzip（单流或多 member）。空字符串表示空 WARC，返回空配对列表。 |

配对规则：

- 配对**只依据**显式的 `WARC-Concurrent-To`：request 与
  response/revisit **任一侧**声明即建立配对，按与归档内 ID 相同的规范化
  规则（去空白、去尖括号）指向另一侧的 `WARC-Record-ID`，**允许前向
  引用**（目标可位于归档任意位置）；双方互指且一致时收敛为同一对；
- **仅 URL 或抓取时间相同不构成配对**；`warcinfo`/`metadata` 等其余类型
  不参与配对，指向它们等同于关联目标不存在；
- 合法配对为 **request ↔ response/revisit**；同一记录重复声明**同一**
  伙伴（同值重复头）自动去重，不算多个伙伴；
- 关联异常一律 **422** 并指出相关 ID：同一记录涉及多个伙伴
  （`multiple_partners`）、双方指向不同伙伴（`inconsistent_partners`）、
  相关 ID 重复（`duplicate_record_id`）、关联目标不存在
  （`concurrent_target_not_found`）、类型不符
  （`concurrent_type_mismatch`），详见错误处理一节。

响应（`200`）：

| 字段                        | 含义 |
|-----------------------------|------|
| `count`                     | 配对成功的 HTTP 交换数 |
| `pairs`                     | 配对结果，按 **request 记录在归档中的顺序**；每项见下 |
| `pairs[].request_id`        | request 记录的 `WARC-Record-ID` 原始值 |
| `pairs[].response_id`       | 响应侧（response 或 revisit）记录的 `WARC-Record-ID` 原始值 |
| `pairs[].target_uri`        | 目标 URL，取**响应侧**记录的 `WARC-Target-URI` |
| `pairs[].method`            | HTTP 请求方法（请求行原值） |
| `pairs[].request_target`    | HTTP 请求目标（请求行原值，如 `/index.html?x=1`） |
| `pairs[].http_status`       | 响应状态码；revisit 自身无 HTTP 头时为 `null` |
| `unpaired_requests`         | 未配对的 request 记录 ID（原始值），按归档原序 |
| `unpaired_responses`        | 未配对的 response/revisit 记录 ID（原始值），按归档原序 |

#### 示例

构造一份归档：request `<urn:uuid:q1>` 通过 `WARC-Concurrent-To` 指向
**其后**的 response `<urn:uuid:s1>`（前向引用），另有一条未配对的
request `<urn:uuid:q2>`：

```bash
python3 - <<'EOF' > sample-exchanges.warc
import sys

def rec(rid, rtype, block, extra=b"", uri="http://example.com/"):
    return (
        f"WARC/1.1\r\nWARC-Type: {rtype}\r\nWARC-Record-ID: {rid}\r\n"
        f"WARC-Date: 2020-01-01T00:00:00Z\r\nWARC-Target-URI: {uri}\r\n"
        f"Content-Length: {len(block)}\r\n".encode()
        + extra + b"\r\n" + block + b"\r\n\r\n"
    )

req = b"GET /home HTTP/1.1\r\nHost: example.com\r\n\r\n"
resp = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello"
out = rec("<urn:uuid:q1>", "request", req,
          b"Content-Type: application/http; msgtype=request\r\n"
          b"WARC-Concurrent-To: <urn:uuid:s1>\r\n")
out += rec("<urn:uuid:s1>", "response", resp,
           b"Content-Type: application/http; msgtype=response\r\n")
out += rec("<urn:uuid:q2>", "request", req,
           b"Content-Type: application/http; msgtype=request\r\n",
           uri="http://example.com/orphan")
sys.stdout.buffer.write(out)
EOF

curl -s http://localhost:8000/v1/http-exchanges \
  -H 'Content-Type: application/json' \
  -d "{\"warc\":\"$(base64 -w0 sample-exchanges.warc)\"}"
```

响应（`<urn:uuid:q1>` 与 `<urn:uuid:s1>` 配对成功，`<urn:uuid:q2>` 未
配对；同 URL 的 `<urn:uuid:q2>` 不会因 URL 相同而被配给任何响应）：

```json
{
  "count": 1,
  "pairs": [
    {
      "request_id": "<urn:uuid:q1>",
      "response_id": "<urn:uuid:s1>",
      "target_uri": "http://example.com/",
      "method": "GET",
      "request_target": "/home",
      "http_status": 200
    }
  ],
  "unpaired_requests": ["<urn:uuid:q2>"],
  "unpaired_responses": []
}
```

### `POST /v1/member-pack`

把 Base64 WARC 打包成**可按记录定位的多 member gzip**（WARC 社区常用的
WARC.GZ 形式：每条记录一个独立 gzip member，顺序拼接，可用压缩流中的
字节偏移随机取出单条记录）。输入归档完整经过与 `/v1/replay` 相同的解码
（2 MiB 上限、gzip 嗅探）与结构/引用链校验——任何一处错误都使整个请求
返回 422。

请求体：

| 字段   | 类型   | 说明 |
|--------|--------|------|
| `warc` | string | **Base64 编码**的 WARC；解码后 **≤ 2 MiB**，支持 gzip（单流或多 member）。空字符串表示空 WARC，返回空包。 |

打包规则：

- **只收录** `response`/`revisit` 记录（`warcinfo`/`request` 等类型丢弃），
  按**归档原序**为每条记录生成一个**独立的 gzip member**；单个 member
  解压后是**完整 WARC 记录 + 记录分隔符（`CRLF CRLF`）**——单独切出一个
  member 即可独立解析；整包解压后各记录间也以标准分隔符相接；
- WARC 头、HTTP 头与负载**原字节保留**（不解 `Content-Encoding`、不拆
  chunked）；
- 归档**末记录缺 `Content-Length`**（其记录块按规则延伸至归档 EOF）时，
  补入**准确块长**（块为空即 `Content-Length: 0`），使 member 块外的
  分隔符不会进入记录块；除该补长行外字节不动，`/v1/replay` 还原的正文
  与原包逐字节一致；
- 所有 gzip member 使用**固定时间戳（MTIME=0）**与固定压缩级别、不带
  文件名等可选头，**同一输入永远产出逐字节相同的输出**；
- 打包结果（解压后）超过 **2 MiB** → **422** `payload_too_large`；
- 交付前对整包解压结果再做一次完整回放校验并与输入逐条比对，回放结果
  （状态、正文、正文来源 ID）不一致时返回 **422** `invalid_member_pack`，
  不交付伪成功的 `gzip_warc`。

响应（`200`）：

| 字段                  | 含义 |
|-----------------------|------|
| `count`               | 收录的 response/revisit 记录数 |
| `gzip_warc`           | 多 member gzip 的 Base64 编码（每记录一个 member，顺序拼接） |
| `members`             | 各记录 member 的压缩字节范围，按归档原序 |
| `members[].record_id` | 该 member 对应记录的 `WARC-Record-ID` 原始值 |
| `members[].offset`    | member 在 `gzip_warc` 中的起始字节偏移 |
| `members[].length`    | member 的压缩字节长度；各 member 范围**连续且互不重叠**，`gzip_warc[offset:offset+length]` 切出后可单独解压 |

`offset` 从 0 开始，`members[i].offset + members[i].length ==
members[i+1].offset`，最后一个 member 的结束位置恰为 `gzip_warc` 全长。

#### 示例

归档含 response `<urn:uuid:aaaa>`（正文 `hello`）与引用它的 revisit
`<urn:uuid:bbbb>`：

```bash
curl -s http://localhost:8000/v1/member-pack \
  -H 'Content-Type: application/json' \
  -d "{\"warc\":\"$(base64 -w0 sample.warc)\"}"
```

响应（每个 member 解压后是一条完整记录加 `CRLF CRLF`；MTIME 固定，
重复请求结果逐字节相同）：

```json
{
  "count": 2,
  "gzip_warc": "H4sIAAAAAAACAzWOwQrCMBBE74H8Q34gTVo8LSKIPVgUlBIpeAvt0gptEtIt6N+bSh3mMAw7j22O9UnlWc5Zk5I0n4AgIs7Buxm3ssbWx05WJYj9Eh0sy6sDm3TYDkpLaVXoQkudJxut4efnH2tjjyQfdQViIAqgFL7tFEbMWj8pzk7eETqSV3Q9DYm144yzszH39bmE1uJ2WasBx9Gv4Qvp/RhUuQAAAB+LCAAAAAAAAgNNjUELgkAQhe+C/2H/wLi7HjoMEYRehE5iCN02nVTQXVnHqH/fUha9eYfHMG+++lhmUic6juqQoHrOhMLTfVgG3nYlNc63UOQo9qu3uK5Di9egw3aQGw6lVKUa1A6UrpTCty/fr8Z3xHAuCxQ984xS0sNM80hJ4yYZR5mzTJbhRLbjHoX6oW/kF6jcP9oEBfRnXmeEE8zAAAAA",
  "members": [
    {"record_id": "<urn:uuid:aaaa>", "offset": 0, "length": 169},
    {"record_id": "<urn:uuid:bbbb>", "offset": 169, "length": 164}
  ]
}
```

按 `offset`/`length` 切出单个 member 可独立解压、独立解析；整包 Base64
解码后（多 member gzip）直接交给 `/v1/replay`，得到的状态、正文与
`body_source_id` 与原包完全一致：

```bash
base64 -d <(echo '上面的 gzip_warc') | gzip -c -d | base64 -w0   # 解压后再 Base64
# 把结果作为 warc 字段 POST 给 /v1/replay
```

## 错误处理（HTTP 422 / 404）

输入与结构错误统一返回 **422**；`/v1/navigate` 与 `/v1/page-assets` 在某一跳
无合格抓取时返回 **404**。响应体形如：

```json
{
  "error": "reference_cycle",
  "message": "记录 <urn:uuid:v1> 的 WARC-Refers-To 引用链成环",
  "record_id": "<urn:uuid:v1>",
  "chain": ["<urn:uuid:v1>", "<urn:uuid:v2>", "<urn:uuid:v3>", "<urn:uuid:v2>"]
}
```

十个接口共用的 WARC 错误（`/v1/snapshot-diff` 对两份归档各自独立校验，
先旧后新；`/v1/merge` 同样对左、右两包各自独立校验，先左后右）：

| `error`                        | 触发条件                                         | 附加字段 |
|--------------------------------|--------------------------------------------------|----------|
| `invalid_base64`               | `warc` 不是合法 Base64                           | - |
| `payload_too_large`            | 解码/解压后超过 2 MiB                            | `decoded_size`, `max_bytes` |
| `malformed_warc`               | 畸形 WARC（非 WARC 首行、头部截断、字段行损坏、gzip 损坏、记录块短于声明的 `Content-Length`（含末记录头完整但头后直接 EOF 的 0 字节截断，即使它跟在完整记录之后）等） | `record_order` / `offset` / `record_id`（若可定位）；块截断时另附 `declared_length`、`actual_length` |
| `missing_record_id`            | response/revisit 缺少 `WARC-Record-ID`           | `record_order`, `record_type` |
| `duplicate_record_id`          | `WARC-Record-ID` 在包内重复                      | `record_id`, `first_record_order`, `duplicate_record_order` |
| `chain_broken`                 | 链中的某条 revisit 缺少 `WARC-Refers-To`         | `record_id`, `broken_record_id`, `chain` |
| `target_not_found`             | `WARC-Refers-To` 指向包内不存在的记录（缺失目标）| `record_id`, `missing_id`, `chain` |
| `reference_cycle`              | 引用链成环（含自引用）                           | `record_id`, `chain` |
| `endpoint_without_http_headers`| 引用链终点 response 没有有效的 HTTP 响应头       | `record_id`, `endpoint_record_id`, `chain` |
| `response_without_http_headers`| 直接出现的 response 记录没有有效的 HTTP 响应头   | `record_id`, `record_order` |

`/v1/extract` 额外的错误：

| HTTP | `error`             | 触发条件                                       | 附加字段 |
|------|---------------------|------------------------------------------------|----------|
| 422  | `unknown_record_id` | `record_ids` 中的 ID 在归档的 response/revisit 记录中不存在 | `record_id` |

`record_ids` 的数量（1～100）与元素类型（字符串）由请求模型校验，不满足时
同样返回 422。

`/v1/digest-audit` 额外的错误（按归档顺序、每条记录内先 Block 后 Payload
的顺序检查，首个错误即返回）：

| HTTP | `error`                        | 触发条件                                       | 附加字段 |
|------|--------------------------------|------------------------------------------------|----------|
| 422  | `invalid_digest_format`        | 摘要值不是 `<算法>:<Base32>` 形式、Base32 字符非法（含出现填充符 `=`）、或长度与算法不符（sha1 为 32 字符、sha256 为 52 字符） | `record_id`, `record_order`, `field`, `value` |
| 422  | `unsupported_digest_algorithm` | 摘要算法不在 sha1/sha256 之中                  | `record_id`, `record_order`, `field`, `algorithm` |

其中 `field` 为出错字段名（`WARC-Block-Digest` 或
`WARC-Payload-Digest`）。

`/v1/capture-index` 额外的错误（`page_size` 的 1～100 与整数类型由请求
模型校验，不满足时同样返回 422；缺 URL 或日期的记录只跳过、不报错，仅
**同时有 URL 与日期但日期非法**的记录触发 `invalid_capture_date`）：

| HTTP | `error`                | 触发条件                                   | 附加字段 |
|------|------------------------|--------------------------------------------|----------|
| 422  | `invalid_capture_date` | 参与列出的记录 `WARC-Date` 非法            | `record_id`, `record_order`, `warc_date` |
| 422  | `invalid_cursor`       | `cursor` 不是合法的 Base64URL JSON、结构/版本非法、绑定的解压后归档 SHA-256 不符（跨包使用），或它指向的排序键在归档中不存在 | `reason`（`malformed` / `unsupported_version` / `archive_mismatch` / `stale_cursor`） |

`/v1/navigate` 额外的错误（其中 `invalid_at`、`invalid_capture_date` 同样
适用于 `/v1/snapshot-diff` 与 `/v1/page-assets`，`invalid_capture_date` 亦
适用于 `/v1/capture-index`；snapshot-diff 不涉及重定向，也没有 404。
`/v1/page-assets` 的**根页面**与 `/v1/navigate` 走同一套选取与
重定向逻辑，下表错误全部原样适用；其**静态资源**的同类失败不会使整个请求
失败，而是逐项标记在响应的 `assets[].error` 中）：

| HTTP | `error`                   | 触发条件                                   | 附加字段 |
|------|---------------------------|--------------------------------------------|----------|
| 422  | `invalid_start_url`       | `url` 缺失或不是绝对 HTTP(S) 地址          | `url` |
| 422  | `invalid_at`              | `at` 不是合法 RFC3339 时点                 | `at` |
| 422  | `invalid_capture_date`    | 参与选择的记录 `WARC-Date` 非法            | `record_id`, `record_order`, `warc_date` |
| 422  | `missing_location`        | 选中的 3xx 记录没有 HTTP `Location` 头     | `url`, `record_id`, `http_status`, `path` |
| 422  | `invalid_redirect_target` | `Location` 解析出的目标不是 HTTP(S)        | `url`, `record_id`, `location`, `resolved_url`, `path` |
| 422  | `redirect_cycle`          | 重定向 URL 成环                            | `url`, `path`（闭合到重复 URL） |
| 422  | `too_many_redirects`      | 选取记录超过 8 跳                          | `url`, `next_url`, `max_hops`, `path` |
| 404  | `capture_not_found`       | 某一跳 URL 在时点前没有合格抓取            | `url`, `at`, `path`（已走过的各跳） |

`chain` 给出完整**引用链**（出错记录自身 ID 起，逐跳包含每个 `WARC-Refers-To`）；
目标缺失时链尾为缺失的目标 ID，可同时通过 `missing_id` 获取。navigate 的
`path` 给出截至出错时已选取的各跳（`url` / `record_id` / `capture_time` /
`http_status`）。

`/v1/merge` 额外的错误（左、右两包的解码与结构/引用链错误均为上表共用
错误；请求模型还会校验 `left_warc` / `right_warc` 字段存在且为字符串）：

| HTTP | `error`             | 触发条件                                       | 附加字段 |
|------|---------------------|------------------------------------------------|----------|
| 422  | `payload_too_large` | 合并后（未压缩）WARC 超过 2 MiB                | `decoded_size`, `max_bytes` |
| 422  | `invalid_merged_warc` | 合并包交付前的回放口径校验未通过（结构/引用链错误，或记录数、顺序、ID 集合与合并预期不一致）；此时不返回 `merged_warc`，避免伪成功 | `cause`（底层错误码）及底层错误的附加字段；不一致时附 `expected_count` / `actual_count` / `expected_ids` / `actual_ids` |

`/v1/http-exchanges` 额外的错误（解码、截断、缺/重复记录 ID、response
HTTP 头与 revisit 引用链等共用错误同上表；`duplicate_record_id` 的重复
判定覆盖 request/response/revisit 全集。配对检查按归档顺序进行，首个
错误即返回）：

| HTTP | `error`                        | 触发条件                                       | 附加字段 |
|------|--------------------------------|------------------------------------------------|----------|
| 422  | `request_without_http_headers` | request 记录缺少有效的 HTTP 请求头或请求行     | `record_id`, `record_order` |
| 422  | `concurrent_target_not_found`  | `WARC-Concurrent-To` 指向的记录在包内不存在（含指向 warcinfo 等不参与配对的类型） | `record_id`, `missing_id` |
| 422  | `concurrent_type_mismatch`     | 关联双方类型不能构成交换（须为 request ↔ response/revisit） | `record_id`, `record_type`, `target_id`, `target_type` |
| 422  | `inconsistent_partners`        | 双方都有声明但指向不同伙伴（A 声明 B，B 声明的却不是 A） | `record_id`, `partner_id`, `partner_points_to` |
| 422  | `multiple_partners`            | 同一记录涉及多个伙伴（自己声明与被他人声明合计超过一个） | `record_id`, `partners`（按伙伴记录归档序） |

`/v1/member-pack` 额外的错误（解码、截断、缺/重复记录 ID、response HTTP
头与 revisit 引用链等共用错误同上表；`warc` 字段缺失或类型不符由请求
模型校验，同样返回 422）：

| HTTP | `error`                | 触发条件                                       | 附加字段 |
|------|------------------------|------------------------------------------------|----------|
| 422  | `payload_too_large`    | 打包结果（解压后）超过 2 MiB                   | `decoded_size`, `max_bytes` |
| 422  | `invalid_member_pack`  | 交付前对整包解压结果的回放校验未通过（结构/引用链错误，或状态、正文、正文来源 ID 与输入不一致）；此时不返回 `gzip_warc`，避免伪成功 | `cause`（底层错误码）及底层错误的附加字段；不一致时附 `expected_count` / `actual_count` |

## 开发与测试

```bash
pip install -r requirements.txt
pip install pytest httpx
pytest -q
```

## 项目结构

```
app/
  main.py          # FastAPI 路由、请求/响应模型、422/404 异常处理
  replay.py        # Base64/gzip 解码、WARC 解析、索引与 revisit 引用链还原
  extract.py       # /v1/extract：按记录 ID 求依赖闭包、按原字节切出子集打包
  navigate.py      # /v1/navigate：时点选取、相对 Location 跟随、成环/跳数判定
  snapshot_diff.py # /v1/snapshot-diff：双归档时点快照与四类 URL 对比
  page_assets.py   # /v1/page-assets：HTML 静态资源提取与逐项可还原性检查
  digest_audit.py  # /v1/digest-audit：Block/Payload 摘要解析与逐条核验
  capture_index.py # /v1/capture-index：排序键分页、cursor 编解码与字节范围
  merge.py         # /v1/merge：双包原序合并、跨包冲突确定性改名与末记录补长
  http_exchanges.py# /v1/http-exchanges：WARC-Concurrent-To 显式配对与交换重建
  member_pack.py   # /v1/member-pack：逐记录独立 gzip member 打包、固定时间戳与定位范围
tests/
  test_replay.py
  test_extract.py
  test_navigate.py
  test_snapshot_diff.py
  test_page_assets.py
  test_digest_audit.py
  test_capture_index.py
  test_merge.py
  test_http_exchanges.py
  test_member_pack.py
Dockerfile
docker-compose.yml
requirements.txt
```
