#!/usr/bin/env python3
"""Relay 接收端参考实现（协议 v2）——用于隔离样本验收，不是生产代码。

只用标准库，演示契约要求的最小行为：
  * 请求标识 = (source, target_date)；内容版本 = report_sha256 + 收到的原始字节哈希；
  * 先落盘再应答；落盘失败返回 stored=false，不留半成品；
  * 同 key 同内容 → 返回原回执（同一 record_id / content_sha256 / stored_at）；
  * 同 key 不同内容 → 409 + content_conflict，旧记录不动；
  * content_sha256 一律对**收到的原始字节**计算，绝不重新序列化 JSON；
  * 提供只读查询 GET /records/<source>/<target_date>，绝不写入。

把它换成真实实现时，行为必须与 `tests/test_relay_receiver_contract.py` 的断言一致。
"""

from __future__ import annotations

import hashlib
import json


class ReferenceReceiver:
    """内存版参考实现；`mode` 用来模拟落盘失败等隔离样本场景。"""

    def __init__(self, secret: str = "", mode: str = "normal"):
        self.secret = secret or "fixture-secret"
        self.mode = mode
        self.records: dict[tuple[str, str], dict] = {}
        self.writes = 0
        self.queries = 0
        self._clock = 0

    # ------------------------------------------------------------------ 工具

    def _now(self) -> str:
        self._clock += 1
        return f"2026-09-21T00:0{self._clock}:00+08:00"

    def _key(self, body: dict) -> tuple[str, str]:
        contract = body.get("research_contract") or {}
        return str(body.get("source", "nng8")), str(contract.get("target_date"))

    # -------------------------------------------------------------- 投递入口

    def post(self, raw: bytes, signature: str | None = None, timestamp: str | None = None):
        """返回 (http_status, response_dict)；raw 必须是发送端实际发出的字节。"""
        body = json.loads(raw.decode("utf-8"))
        contract = body.get("research_contract") or {}
        source = str(body.get("source", "nng8"))
        target_date = str(contract.get("target_date"))
        as_of = str(contract.get("as_of"))
        report_sha256 = str(contract.get("report_sha256"))
        content_sha256 = hashlib.sha256(raw).hexdigest()          # 原始字节，不重新序列化
        key = (source, target_date)

        if self.mode == "store_failure":
            return 200, {"status": "accepted", "source": "nng8", "day": as_of,
                         "target_date": target_date, "stored": False}

        existing = self.records.get(key)
        if existing is not None:
            if existing["report_sha256"] != report_sha256 or existing["content_sha256"] != content_sha256:
                return 409, {"status": "rejected", "reason": "content_conflict"}
            # 同 key 同内容：原样回执，不新建、不覆盖。
            return 200, dict(existing["ack"])

        if self.mode == "missing_fields":
            return 200, {"status": "accepted", "source": "nng8", "day": as_of,
                         "target_date": target_date}

        ack = {"status": "accepted", "source": "nng8", "day": as_of,
               "target_date": target_date, "stored": True,
               "content_sha256": content_sha256, "record_id": f"rec-{len(self.records) + 1}",
               "stored_at": self._now()}
        self.records[key] = {"report_sha256": report_sha256, "content_sha256": content_sha256,
                             "ack": ack, "raw": raw}
        self.writes += 1
        return 200, dict(ack)

    # -------------------------------------------------------------- 只读查询

    def get_record(self, source: str, target_date: str):
        """只读：不写、不改、不产生新记录。"""
        self.queries += 1
        record = self.records.get((source, target_date))
        if record is None:
            return 404, {"status": "not_found"}
        return 200, dict(record["ack"])
