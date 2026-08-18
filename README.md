# AIHOT Data Bridge V0

一个很薄的 HTTP 数据桥：稳定读取 AIHOT 官方 API，在 API 失败时使用对应 RSS，完成分页、规范化、确定性去重和逐源 coverage 报告。它为 ChatGPT Scheduled Task 提供一个固定入口，但不做 AI 总结、邮件、调度或存储。

## 上游数据源

API：

- `GET /api/v1/items?mode=selected&window=24h&limit=100`
- `GET /api/v1/items?mode=all&window=24h&limit=100`
- `GET /api/v1/items?mode=all&window=24h&category=paper&limit=100`
- `GET /api/v1/hot-topics`
- `GET /api/v1/dailies/latest`

仅在对应 API 失败后使用的 RSS：

- `/feed.xml`（selected）
- `/feed/all.xml`（all）
- `/feed/category/paper.xml`（paper）
- `/feed/daily.xml`（daily）

`hot_topics` 没有对应的官方 RSS；其 API 失败时会明确报告 `failed`。

2026-08-17 实测的 API Schema：items 响应根字段为 `schemaVersion/items/page/query`，分页游标位于 `page.nextCursor`，下一页通过 `cursor` 参数请求；日报主体位于 `report`；item 的内容发布时间和 AIHOT 发现时间分别为 `publishedAt` 和 `discoveredAt`。RSS 为 RSS 2.0，稳定 ID 位于 `guid`。代码以这些真实响应为准，没有按提示词猜字段。

## Bridge API

### `GET /health`

仅检查 Bridge 进程：

```json
{"status":"ok"}
```

### `GET /aihot/today`

实时读取五路上游并返回：

- `window`：本次 rolling 24h 的明确起止时间；
- `coverage`：每一路的独立状态、实际来源、条数和错误；
- `summary.raw_items`：五路规范化后、去重前的记录数；
- `summary.deduplicated_items`：确定性 item-level 去重后的记录数；
- `items`：统一字段和保留的上游 metadata。

输出中的 `published_at` 来自 `publishedAt`，`collected_at` 来自 `discoveredAt`，两者不会互相替代。不同形状通过 `item_type`（`item`、`hot_topic`、`daily_report`）区分；同一条目出现在哪些上游通过 `source_channels` 保留。

### Coverage 语义

- `ok`：对应 API 完整成功；
- `fallback`：API 失败，但对应 RSS 成功；
- `partial`：拿到部分 API 数据，但达到最大页数或遇到重复游标；
- `failed`：API 与可用的对应 RSS 都失败。

单路失败不会让 `/aihot/today` 返回 500；失败会留在该路 coverage 中。只有 Bridge 自身无法执行时才返回 5xx。

## 启动

需要 Python 3.11+：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m uvicorn aihot_bridge.main:app --host 127.0.0.1 --port 8000
```

可选环境变量：

- `AIHOT_BASE_URL`
- `AIHOT_CONNECT_TIMEOUT_SECONDS`（默认 5）
- `AIHOT_REQUEST_TIMEOUT_SECONDS`（默认 20）
- `AIHOT_MAX_RETRIES`（默认 2，即首次请求后最多重试两次）
- `AIHOT_MAX_PAGES`（默认 10）

429 会尊重 `Retry-After`；5xx 和网络/超时错误使用有限退避重试。除 429 外的普通 4xx 不盲目重试。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

测试覆盖：五个 API 全成功、paper RSS fallback、API 与 RSS 同时失败仍返回 200、分页、stable ID 与 URL 去重、发布时间/采集时间隔离、429 `Retry-After`、5xx 恢复、最大页数标记 `partial`，以及 health endpoint。

## 保留的动态部署资产

`Dockerfile`、`.gcloudignore` 和 `.dockerignore` 仅保留为未来动态部署备选。本项目当前不配置 Cloud Run、Google Cloud 项目或 Billing；计划中的公开传输路径是下面的 GitHub Pages 静态快照。

对外只暴露 `GET /health` 和 `GET /aihot/today`；FastAPI 的 `/docs`、`/redoc` 和 `/openapi.json` 已关闭。

## Static Snapshot Deployment

GitHub Actions 定时调用与 FastAPI 相同的 `BridgeService.today()` 核心，将 rolling 24h 结果原子写入 `dist/today.json`，再通过 GitHub Pages 发布。Pages 只是 transport layer；分页、fallback、coverage、时间映射和去重仍全部来自现有 Python 核心。

本地生成和验证：

```powershell
.\.venv\Scripts\python.exe -m aihot_bridge.snapshot --output dist\today.json
.\.venv\Scripts\python.exe -m aihot_bridge.snapshot --check dist\today.json
```

部署 workflow 位于 `.github/workflows/snapshot-pages.yml`：

- 每小时第 23、53 分钟运行，避开整点高峰，理论最大快照年龄约 30 分钟；
- 支持从 GitHub Actions 页面手工执行 `workflow_dispatch`；
- 同类任务并发时取消旧任务；
- 15 分钟内未完成 snapshot build 则失败；
- Pages deployment 遇到瞬时失败时只重试一次；
- deploy 后回读固定公网 URL，确认公开 `generated_at` 已达到本次候选；
- 只使用 GitHub 官方 Pages artifact 和 OIDC actions。

`.github/workflows/snapshot-health.yml` 在每小时第 13、43 分钟只读取公开 `today.json`，不抓取 AIHOT；快照超过 90 分钟、HTTP 失败或 `generated_at` 无效时 workflow 失败。两个 schedule 都是 GitHub best-effort 调度，并非准点执行保证。

部分上游失败仍会发布，并在 `coverage` 中明确保留 `fallback`、`partial` 或 `failed`。如果 `selected/all/paper` 全部没有可信条目，exporter 会失败且不替换上一次成功 Pages 部署，避免用新的 `generated_at` 发布一个误导性的空快照。

部署后的固定地址：

```text
https://ninaix0217.github.io/aihot-data-bridge/today.json
```

该固定 URL 已于 2026-08-18 再次通过 `workflow_dispatch` 真实部署及部署后回读验证：HTTPS 返回 200，JSON 可解析，且公开 `generated_at` 与本次 artifact 一致。

### Freshness contract

`generated_at` 是本次 snapshot 抓取完成的 UTC ISO 8601 时间。建议 ChatGPT Scheduled Task 在读取 items 前先判断：

- age <= 90 minutes：`FRESH`
- 90 minutes < age <= 3 hours：`STALE_BUT_USABLE`
- age > 3 hours：`UNRELIABLE`

快照不是实时 API；下游还应检查五路 `coverage`。FastAPI 继续保留为本地调试、契约验证和未来动态部署入口。

## 已知限制

- V0 不做缓存或持久化，每次 `/aihot/today` 请求都会实时读取上游。
- RSS 只有 feed 自身提供的字段；没有 `discoveredAt` 时，`collected_at` 保持 `null`，不会用 Bridge 抓取时间伪造。
- RSS feed 的条数可能受官方 feed 上限约束；coverage 会标记为 `fallback`，不会伪装成 API 完整成功。
- 去重仅使用 ID、URL/canonical URL、标题 + 来源 + 发布时间，不做事件级语义合并。
- GitHub scheduled workflows 可能延迟或在高负载时丢弃；公开仓库连续 60 天无活动时，schedule 会被 GitHub 自动停用。
- 除 GitHub Actions 定时生成和 Pages 静态发布外，不包含认证、数据库、额外调度服务、Gmail 或 AI 功能。
