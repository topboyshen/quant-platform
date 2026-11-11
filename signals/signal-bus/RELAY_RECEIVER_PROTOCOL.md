# Relay 接收端协议 v2（nng8 → DediRock）

本文件是**对接契约**：发送端（signal-bus `scripts/research_relay.py`）与接收端（DediRock）必须
按同一套字段与语义实现。v2 相对 v1 只增加：日期语义、幂等规则、`accepted` 补证方式、
以及“字节级内容比较”的硬要求；**信封与 HMAC 方式不变**。

## 1. 日期语义（不得混用）

| 名称 | 含义 | 出现位置 |
|---|---|---|
| `target_date` | **报告日期**：这次投递对应的那份晨报所属交易日 | 请求体 `research_contract.target_date`；v2 回执 `target_date` |
| `as_of` | **数据截止日**：报告里行情数据覆盖的交易日（通常为上一交易日） | 请求体 `research_contract.as_of`；回执 `day` |

规则：

1. 回执 `day` **回显 `as_of`**，不是报告日期；接收端不得把 `day` 当作“报告日期”存储或展示。
2. 接收端记录必须同时保存 `target_date` 与 `as_of`，并在查询接口里原样返回。
3. 两者关系：`as_of` 必须早于 `target_date`；接收端只需校验“都是 `YYYY-MM-DD` 且 `as_of <= target_date`”，
   不需要推断交易日历（节假日判定由发送端负责）。

## 2. 请求标识与幂等

**请求标识（delivery key）＝ `source` + `target_date`**，即“同一天只允许一条投递记录”。
内容版本由 `report_sha256`（密封报告哈希）与 `content_sha256`（本次发送字节哈希）标识。

| 情形 | 接收端必须做什么 | 回执 |
|---|---|---|
| 同 key + **相同内容**（`report_sha256` 与字节哈希都相同） | 返回**原回执**：同一个 `record_id`、同一 `content_sha256`、原 `stored_at`；**不新建、不覆盖** | `status=accepted, stored=true, content_sha256=<原值>, record_id=<原值>` |
| 同 key + **内容不同**（`report_sha256` 或字节哈希不同） | **拒绝**，保留旧记录不动 | HTTP 409，`{"status":"rejected","reason":"content_conflict"}` |
| 同 key + 旧记录已存在但内容相同、旧记录**未落盘成功** | 允许重试写入（这是“存储失败后的重试”，不是新投递） | 同“正常存储” |

发送端对 `rejected`（含 `content_conflict`）一律记为**终态失败**：不重试、不重发、不覆盖。

## 3. 回执字段（v2）

成功且已落盘：

```json
{
  "status": "accepted",
  "source": "nng8",
  "day": "<as_of>",
  "target_date": "<报告日期>",
  "stored": true,
  "content_sha256": "<64 位小写十六进制>",
  "record_id": "<接收端不透明字符串>",
  "stored_at": "<带时区的 ISO 8601，例如 2026-09-21T00:02:11+08:00>"
}
```

允许出现的其他形态：

| 情形 | HTTP | 回执 | 发送端判定 |
|---|---|---|---|
| 落盘失败（明确） | 200 | `{"status":"accepted","source":"nng8","day":...,"stored":false}` | `failed`（终态，不重发） |
| 内容冲突 | 409 | `{"status":"rejected","reason":"content_conflict"}` | `rejected`（终态） |
| 认证/格式/大小/冲突等拒绝 | 400/401/403/404/405/**409**/410/413/415/422/429 | 任意安全正文 | `rejected`（终态） |
| 服务端错误 / 网络中断 | 5xx / 无响应 | — | `ambiguous`（人工复核，不自动重试） |
| 已接受但缺 `stored` 或 `content_sha256` | 200 | 上述字段缺失 | `accepted`（**未确认**，见 §4） |

接收端**不得**在回执里返回存储路径、凭据、内部错误原文；`path` 等私有字段发送端不读取、不落盘。

## 4. 内容比较必须是字节级

发送端对**实际 POST 的 JSON 字节**计算 `payload_sha256 = sha256(body)`，并与回执 `content_sha256` 比较。

因此接收端必须：

1. **保存（或在校验时使用）收到的原始字节**，并对这些字节计算 `content_sha256`；
2. **不得**解析 JSON → 重新序列化 → 对重新序列化的结果取哈希（键顺序、空白、数字格式都会变，
   那样比较的不是同一个对象）；
3. 如果接收端有自己的包装格式，必须在文档里写明包装规则，并且包装结果对同一输入字节**可复现**
   （能对已保存内容重新算出同一个哈希）。

## 5. `accepted` 如何补证（禁止靠重发取回执）

当发送端只有“HTTP 已接受、存储未确认”时，**只能**通过只读查询补证：

```
GET /records/<source>/<target_date>
X-Timestamp: <epoch 秒>
X-Signature: sha256=<HMAC_SHA256(secret, timestamp + ".GET." + "/records/nng8/<target_date>")>
X-Source: nng8
```

响应（200）：

```json
{
  "status": "accepted",
  "source": "nng8",
  "day": "<as_of>",
  "target_date": "<报告日期>",
  "stored": true,
  "content_sha256": "<64 位小写十六进制>",
  "record_id": "<原 record_id>",
  "stored_at": "<带时区 ISO 8601>"
}
```

规则：

1. 查询**只读**：不得触发写入、不得改变记录、不得产生新的投递；
2. 未找到记录 → 404（发送端保持“未确认”，不重发）；
3. 查询结果与发送端保存的 `payload_sha256` 一致且 `stored=true` → 发送端把本地状态升级为
   `confirmed`（`confirmed_via: read_only_query`），**不发送任何东西**；
4. **禁止**为了拿到回执而重新 POST：重发会命中 §2 的冲突规则，且可能造成重复投递。

## 6. 接收端隔离验收（先用样本，通过后再部署发送端）

用 `tests/test_relay_receiver_contract.py` 里的参考实现与样本做对照，五类必须全部通过：

| # | 场景 | 期望 |
|---|---|---|
| 1 | 正常存储 | 200 + `stored=true` + `content_sha256` 等于**请求体字节**哈希 + `record_id` |
| 2 | 落盘失败 | 200 + `stored=false`；不产生半成品记录；发送端记 `failed` |
| 3 | 重复请求（同 key 同内容） | 返回**同一** `record_id`/`content_sha256`/`stored_at`；不新建记录 |
| 4 | 内容冲突（同 key 不同内容） | 409 + `content_conflict`；旧记录保持原样 |
| 5 | 回执缺字段（无 `stored` / 无 `content_sha256` / 无 `record_id`） | 发送端只能记“未确认”，不得判通过 |

发送端侧的期望行为已由离线测试锁定；接收端通过后，再部署 `research_relay.py`。
旧记录（历史 HTTP 已接受、存储未确认）保持原样，看板继续标注“历史 HTTP 接受，存储未确认”。
