# concepts.error 最小修复说明

日期：2026-09-08。本文及两个新增 Python 文件由独立子任务在本地准备；生产集成、联合测试、提交和部署由主任务负责。

## 已确认的故障与证据边界

- 现有 `concept_blocks` 使用 `urllib.request` 请求百度既有 `getrelatedblock` HTTPS 接口。
- 在 VPS 现有 research-data venv 做有界只读探测：原请求对 `002617` 返回 HTTP 403。
- 同来源、同证券、同 `market=ab`、`type=stock`、`finClientType=pc` 的普通 `requests.Session` 直连返回 HTTP 200。
- `002617`、`600875` 均返回 `ResultCode="0"`，`Result[code]` 为 3 个类别列表；没有输出完整业务响应。
- 主任务额外只读提取既有 8 月 28 日成功快照的类别键：`002617`、`300033`、`600875` 均为 `地域/概念/行业`，无需增加 API 探测。
- 单独对原 urllib URL 加编码，或补标准 Accept 请求头，仍返回 403。因此不能断言只有 URL 编码有问题；可确认是旧请求路径/客户端与该端点存在兼容性故障，服务端拒绝策略未获证实。
- 新版未更换来源、查询范围或数据含义，不循环变换身份、不切换代理、不重试 403，也不关闭 TLS 校验。

## 新增接口与行为

`ConceptClient` 支持 context manager；一个 Session 可依次调用 `concepts(code)`，输出仍为 `{类别名: [条目名, ...]}`。

- `requests` 已是项目运行依赖，不引入新依赖。
- 固定原 HTTPS 端点；让客户端通过 `params` 编码参数，不手工拼接 JSON URL。
- 连接/读取超时 5/15 秒，每个失败请求最多 3 次，退避 1/2 秒。
- 仅超时、连接错误及 HTTP 429/5xx 重试；证书错误、重定向、401/403、JSON/schema/证券错配均失败关闭。
- `ResultCode` 必须是字符串 `"0"` 或整数 `0`；严格匹配所请求证券，防止把其他证券的结果归给当前证券。
- 类别、列表与名称均验证；拒绝重复类别覆盖、缺失条目名称、完全空集合、畸形 JSON、重复 JSON 键及非有限常量。
- 保留上游明确返回的空类别列表，但所有类别均为空不能作为成功证据。
- `概念` 类别必须显式存在：只返回行业/地域而缺少概念是部分响应；明确的 `概念: []` 则保留，不能把字段缺失等同于无概念归属。
- 响应解码后最大 512 KiB，最多 32 类、每类 1000 项、名称 256 字符；超限显式失败，不截断或减少证券集合。
- `ConceptError.diagnostic` 与 `safe_concept_diagnostic` 仅保留 `source/stage/errorType/code/attempt/httpStatus` 白名单，不记录原异常、响应正文、完整 URL、请求头或凭证。
- 无数据库、文件、任务、模型或通知副作用；不生成/改写时间戳，真实观察时间仍由现有 `collectionWindow` 表述。

## 主任务集成建议

1. 导入 `ConceptClient, ConceptError, safe_concept_diagnostic`。
2. 保留 `concept_blocks(code, *, client=None)` 包装函数以兼容原调用；可传入同一 Session 的 client。
3. `collect_concepts(codes)` 逐证券收集并捕获 `ConceptError`，失败条目为 `{"error": exc.diagnostic}`；不要重抓已成功证券。
4. 父 `validate_source_completeness` 继续拒绝任一失败/缺证券；把合法 concepts 诊断并入 `sourceErrors`，不能让部分成功变成整源成功。
5. 不再使用整组 `collect_source(lambda: ...)` 的重试，否则一只证券失败会反复重抓成功证券。
6. 单独新增集成测试：部分成功保留、全部证券覆盖、单证券最终失败拒绝快照、诊断无原文泄漏、时间语义与只追加不变。

## 验证

- 新文件离线单元测试 `tests/test_concept_source.py`：28 项全部通过，模拟网络，无生产数据库访问；含缺失概念拒绝、明确空概念保留。
- 本地使用已有 Kimi CLI Python 的 requests 2.34.2；系统 Python 与文档运行时没有 requests，未安装包或修改这两个环境。
- 生产 Python/requests 联合测试、全部观察池证券的客户端实测，以及自然运行快照验收，仍由主任务完成后记录。
- 本子任务未改生产文件、daily_snapshot.py、现有测试、代理、cron、业务状态，未调用 daily_snapshot 或触发外发。
