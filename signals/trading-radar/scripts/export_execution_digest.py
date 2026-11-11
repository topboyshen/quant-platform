#!/usr/bin/env python3
"""Human-readable previous-day execution digest for dedirock 08:00 merge."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import ssl
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path("/home/ubuntu/trading-radar")
DB = ROOT / "data/approved-signals/intake.sqlite3"
OUT_DIR = ROOT / "data/approved-signals/digests"
TZ8 = timezone(timedelta(hours=8))
NOISE_RE = re.compile(r"(selftest|test:|probe|deploy-test|tls-test|443-test|WireTest)", re.I)


def is_noise(key: str, payload: dict) -> bool:
    prov = payload.get("provenance")
    src = ""
    if isinstance(prov, dict):
        src = str(prov.get("source") or "")
    blob = " ".join([key or "", str(prov or ""), src, str(payload.get("reviewer") or "")])
    return bool(NOISE_RE.search(blob))


def main() -> None:
    now = datetime.now(TZ8)
    day = (now - timedelta(days=1)).date().isoformat()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    rows = []
    if DB.exists():
        conn = sqlite3.connect(str(DB))
        cur = conn.execute(
            "SELECT idempotency_key, received_at, status, detail, payload_json FROM intakes WHERE substr(received_at,1,10)=?",
            (day,),
        )
        for key, received_at, status, detail, payload_json in cur.fetchall():
            try:
                payload = json.loads(payload_json)
            except Exception:
                payload = {}
            if is_noise(key, payload):
                continue
            rows.append(
                {
                    "idempotency_key": key,
                    "received_at": received_at,
                    "status": status,
                    "detail": detail,
                    "symbol": payload.get("symbol"),
                    "side": payload.get("side"),
                    "signal_type": payload.get("signal_type"),
                    "confidence": payload.get("confidence"),
                }
            )
        conn.close()

    accepted = [r for r in rows if r["status"] == "accepted"]
    rejected = [r for r in rows if r["status"] == "rejected"]

    def fmt_item(r: dict) -> str:
        side = r.get("side") or "—"
        conf = r.get("confidence")
        conf_s = f"{float(conf):.0%}" if isinstance(conf, (int, float)) else "—"
        return f"{r.get('symbol') or '未知'} · {side} · {r.get('signal_type') or '—'} · 置信{conf_s}"

    lines = [
        f"【执行摘要】{day}",
        f"受理 {len(accepted)} 条（风控通过/订单占位 queued_stub，未实盘下单） · 拒绝 {len(rejected)} 条 · 成交 — · 盈亏 未启用",
    ]
    if accepted:
        lines.append("受理明细（最多5条）：")
        for r in accepted[:5]:
            lines.append(f"- {fmt_item(r)}")
    if rejected:
        lines.append("拒绝明细（最多5条）：")
        for r in rejected[:5]:
            reason = r.get("detail") or "风控拒绝"
            lines.append(f"- {fmt_item(r)} （{reason}）")
    if not rows:
        lines.append("昨日无业务受理/拒绝（已过滤测试探针）。")
    markdown = "\n".join(lines) + "\n"

    digest = {
        "schema_version": "0.2",
        "report_type": "execution_digest",
        "as_of_day": day,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "source": "quant",
        "summary": {
            "accepted": len(accepted),
            "rejected": len(rejected),
            "fills": 0,
            "pnl": None,
            "pnl_status": "not_enabled",
            "note": "accepted=queued_stub占位；无实盘下单；成交/盈亏未启用",
        },
        "text": markdown,
        "markdown": markdown,
        "items": rows[:20],
    }

    out = OUT_DIR / f"execution_digest_{day}.json"
    out.write_text(json.dumps(digest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT_DIR / "latest.json").write_text(json.dumps(digest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT_DIR / "latest.md").write_text(markdown, encoding="utf-8")
    print(f"wrote {out} items={len(rows)}")

    url_file = Path(os.environ.get("DEDIROCK_DIGEST_URL_FILE", str(ROOT / "secrets/dedirock_digest_url")))
    secret_file = Path(os.environ.get("DEDIROCK_DIGEST_HMAC_FILE", str(ROOT / "secrets/dedirock_digest_hmac")))
    url = url_file.read_text(encoding="utf-8").strip() if url_file.exists() else ""
    url = os.environ.get("DEDIROCK_DIGEST_WEBHOOK_URL", url).strip()
    if url and secret_file.exists():
        body = json.dumps(digest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ts = str(int(time.time()))
        secret = secret_file.read_text(encoding="utf-8").strip().encode("utf-8")
        sig = "sha256=" + hmac.new(secret, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Timestamp": ts,
                "X-Signature": sig,
                "X-Source": "quant",
            },
            method="POST",
        )
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            print("digest_push", resp.status)
    else:
        print("digest_push_skipped_no_dedirock_webhook")


if __name__ == "__main__":
    main()
