"""One distinct operational alert; never retries the daily report itself."""
from datetime import datetime
import json
import os
from pathlib import Path
import sys

from run_summary import ACCOUNT, CONTROL, STATE, TZ, exclusive_json, validate_control
from delivery import deliver


def notify(root, control, now, sender_factory):
    now = now.astimezone(TZ)
    day = now.date().isoformat()
    target = validate_control(control, day)
    root = Path(root)
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if root.is_symlink() or root.stat().st_mode & 0o077:
        raise ValueError("unsafe_fault_directory")
    # Stable per-day alert content, distinct from a research summary. A sender
    # timeout must never cause either message to be retried.
    text = (f"{day} 研究日报运行告警\n"
            "16:20 日报未取得完整成功回执：可能未生成、发送失败或送达待确认。"
            "请核对独立发送记录与服务日志；不要补跑研究或盲目重发。"
            "原始研究与健康报告仍保留，此告警不代表研究规则变更。")
    record = {"accountId": ACCOUNT, "businessDate": day,
              "kind": "operational_failure", "text": text}
    path = root / (day + "-operational-failure.json")
    try:
        exclusive_json(path, record)
    except FileExistsError:
        if path.is_symlink() or json.loads(path.read_text()) != record:
            raise ValueError("frozen_fault_conflict")
    return deliver(root / "notification-outbox.sqlite3", ACCOUNT,
                   "telegram:" + target, day, "v1-operational-failure",
                   text, sender_factory(target))


if __name__ == "__main__":
    os.umask(0o077)
    try:
        from telegram_once import configured_sender
        receipt = notify(STATE, json.loads(CONTROL.read_text()), datetime.now(TZ), configured_sender)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        raise SystemExit(0 if receipt["status"] == "sent" else 2)
    except Exception as exc:
        print(json.dumps({"status": "failed", "errorClass": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1)
