#!/usr/bin/env python3
"""接收端协议 v2 的隔离样本验收（发送端侧视角）。

对每个场景：把**发送端实际会发出的字节**交给参考接收端，再把它的应答喂回
`research_relay.post_payload` 的分类路径，断言双方判定一致。不联网、不写生产。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import research_relay as relay  # noqa: E402  # 交付包内按仓库布局解析
from reference_receiver import ReferenceReceiver  # noqa: E402

DAY = "2026-09-09"
AS_OF = "2026-09-08"


def payload_dict(target_date: str = DAY, report_sha256: str = "a" * 64) -> dict:
    """与真实 build_payload 同形的载荷：顶层 as_of/target_date/report_sha256 都要有。"""
    return {
        "source": "nng8",
        "target_date": target_date,
        "as_of": AS_OF,
        "report_sha256": report_sha256,
        "research_contract": {"target_date": target_date, "as_of": AS_OF,
                              "report_sha256": report_sha256, "data_status": "partial"},
        "summary": "隔离样本",
    }


def payload_bytes(target_date: str = DAY, report_sha256: str = "a" * 64) -> bytes:
    return relay.encode_payload(payload_dict(target_date, report_sha256))


def classify_conflict(payload: dict):
    """真实 urllib 对 409 会抛 HTTPError：按真实路径分类。"""
    import datetime as dt
    import urllib.error
    opener = Mock()
    opener.open.side_effect = urllib.error.HTTPError("https://example.org/ingest", 409,
                                                     "Conflict", None, None)
    with patch.object(relay.urllib.request, "build_opener", return_value=opener):
        return relay.post_payload(payload,
                                  {"url": "https://example.org/ingest", "secret": "test"},
                                  dt.datetime(2026, 9, 9, 0, 5, tzinfo=dt.timezone.utc))


def classify(response_body: bytes, payload: dict, status: int = 200):
    """把参考接收端的应答喂回发送端分类路径（同一份载荷 → 同一份字节）。"""
    import datetime as dt
    response = Mock(status=status)
    response.read.return_value = response_body
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = response
    with patch.object(relay.urllib.request, "build_opener", return_value=opener):
        return relay.post_payload(payload,
                                  {"url": "https://example.org/ingest", "secret": "test"},
                                  dt.datetime(2026, 9, 9, 0, 5, tzinfo=dt.timezone.utc))


class ReceiverContractTests(unittest.TestCase):
    def test_normal_store_is_confirmed(self):
        receiver = ReferenceReceiver()
        raw = payload_bytes()
        status, ack = receiver.post(raw)
        self.assertEqual(status, 200)
        self.assertTrue(ack["stored"])
        self.assertEqual(ack["content_sha256"], __import__("hashlib").sha256(raw).hexdigest())
        self.assertEqual(ack["day"], AS_OF, "day 回显数据截止日，不是报告日期")
        self.assertEqual(ack["target_date"], DAY, "报告日期单独回显")
        result = classify(json.dumps(ack).encode(), payload_dict())
        self.assertEqual(result["status"], "confirmed")
        self.assertTrue(result["content_match"])

    def test_store_failure_is_an_explicit_terminal_failure(self):
        receiver = ReferenceReceiver(mode="store_failure")
        status, ack = receiver.post(payload_bytes())
        self.assertEqual(status, 200)
        self.assertFalse(ack["stored"])
        self.assertEqual(receiver.writes, 0, "落盘失败不得留下半成品记录")
        result = classify(json.dumps(ack).encode(), payload_dict())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "receiver_reported_not_stored")

    def test_duplicate_request_returns_the_original_receipt(self):
        receiver = ReferenceReceiver()
        raw = payload_bytes()
        _, first = receiver.post(raw)
        _, second = receiver.post(raw)
        self.assertEqual(first["record_id"], second["record_id"])
        self.assertEqual(first["content_sha256"], second["content_sha256"])
        self.assertEqual(first["stored_at"], second["stored_at"])
        self.assertEqual(receiver.writes, 1, "同 key 同内容不得新建记录")
        self.assertEqual(classify(json.dumps(second).encode(), payload_dict())["status"], "confirmed")

    def test_conflicting_content_is_rejected_and_never_overwrites(self):
        receiver = ReferenceReceiver()
        _, first = receiver.post(payload_bytes())
        status, ack = receiver.post(payload_bytes(report_sha256="b" * 64))
        self.assertEqual(status, 409)
        self.assertEqual(ack["reason"], "content_conflict")
        self.assertEqual(receiver.writes, 1)
        _, after = receiver.post(payload_bytes())          # 旧内容仍然可用
        self.assertEqual(after["record_id"], first["record_id"])
        result = classify_conflict(payload_dict(report_sha256="b" * 64))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "http_rejected")

    def test_ack_missing_storage_fields_can_only_be_unconfirmed(self):
        receiver = ReferenceReceiver(mode="missing_fields")
        _, ack = receiver.post(payload_bytes())
        self.assertNotIn("stored", ack)
        self.assertNotIn("content_sha256", ack)
        result = classify(json.dumps(ack).encode(), payload_dict())
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["reason"], "http_accepted_storage_unconfirmed")
        self.assertIsNone(result["content_match"])

    def test_content_hash_must_be_over_the_posted_bytes(self):
        """重新序列化 JSON 后再取哈希会得到不同结果——这正是要禁止的做法。"""
        import hashlib
        raw = payload_bytes()
        receiver = ReferenceReceiver()
        _, ack = receiver.post(raw)
        reserialized = json.dumps(json.loads(raw.decode()), indent=2).encode()
        self.assertNotEqual(hashlib.sha256(reserialized).hexdigest(), ack["content_sha256"])
        self.assertEqual(ack["content_sha256"], hashlib.sha256(raw).hexdigest())

    def test_read_only_query_confirms_without_sending(self):
        receiver = ReferenceReceiver()
        raw = payload_bytes()
        _, ack = receiver.post(raw)
        self.assertEqual(receiver.queries, 0)
        status, record = receiver.get_record("nng8", DAY)
        self.assertEqual(status, 200)
        self.assertEqual(receiver.writes, 1, "查询不得写入")
        self.assertEqual(record["record_id"], ack["record_id"])
        # 发送端只读分类
        sent = __import__("hashlib").sha256(raw).hexdigest()
        response = Mock(status=200)
        response.read.return_value = json.dumps(record).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(relay.urllib.request, "build_opener", return_value=opener):
            probed = relay.query_record({"url": "https://example.org/ingest", "secret": "test"},
                                        DAY,
                                        __import__("datetime").datetime(2026, 9, 9, 0, 5,
                                                                         tzinfo=__import__("datetime").timezone.utc),
                                        sent, data_date=AS_OF)
        self.assertEqual(probed["status"], "confirmed")
        self.assertEqual(probed["receiver_content_sha256"], sent)
        self.assertEqual(opener.open.call_args.args[0].get_method(), "GET")

    def test_query_404_stays_unconfirmed(self):
        import urllib.error
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            "https://example.org/records/nng8/" + DAY, 404, "Not Found", None, None)
        with patch.object(relay.urllib.request, "build_opener", return_value=opener):
            probed = relay.query_record({"url": "https://example.org/ingest", "secret": "test"},
                                        DAY,
                                        __import__("datetime").datetime(2026, 9, 9, 0, 5,
                                                                         tzinfo=__import__("datetime").timezone.utc),
                                        "a" * 64)
        self.assertEqual(probed["status"], "not_found")


if __name__ == "__main__":
    unittest.main()
