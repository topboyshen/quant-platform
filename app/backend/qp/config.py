"""Runtime configuration from environment (systemd EnvironmentFile=/etc/qp/qp.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    backup_dir: str
    backup_keep_days: int
    session_ttl_hours: int
    cookie_name: str
    cookie_path: str
    cookie_secure: bool
    api_health_url: str
    notify_mode: str  # default enqueue mode: "shadow" | "live"
    signals_notify_mode: str  # M4 live switch; "shadow" | "live"
    notify_kill_switch: bool
    notify_disabled_file: str
    notify_max_attempts: int
    telegram_bot_token_file: str
    telegram_chat_id_file: str
    telegram_api_base: str
    telegram_proxy: str | None
    telegram_timeout_sec: float
    approved_signals_hmac_file: str
    approved_signals_max_skew_sec: int
    execution_digest_hmac_file: str
    execution_digest_output_dir: str
    record_chain: bool  # M10 ETF record-chain dual-write switch


def get_settings() -> Settings:
    return Settings(
        database_url=os.getenv("QP_DATABASE_URL", "postgresql+psycopg:///qp"),
        backup_dir=os.getenv("QP_BACKUP_DIR", "/var/lib/qp/backups"),
        backup_keep_days=int(os.getenv("QP_BACKUP_KEEP_DAYS", "14")),
        session_ttl_hours=int(os.getenv("QP_SESSION_TTL_HOURS", "168")),
        cookie_name=os.getenv("QP_COOKIE_NAME", "qp_session"),
        cookie_path=os.getenv("QP_COOKIE_PATH", "/next/"),
        cookie_secure=os.getenv("QP_COOKIE_SECURE", "1") == "1",
        api_health_url=os.getenv("QP_API_HEALTH_URL", "http://127.0.0.1:18800/api/health"),
        notify_mode=os.getenv("QP_NOTIFY_MODE", "shadow"),
        signals_notify_mode=os.getenv("QP_SIGNALS_NOTIFY_MODE", "shadow"),
        notify_kill_switch=os.getenv("QP_NOTIFY_KILL_SWITCH", "0").strip().lower()
        in {"1", "true", "yes", "on"},
        notify_disabled_file=os.getenv("QP_NOTIFY_DISABLED_FILE", "/etc/qp/notify.disabled"),
        notify_max_attempts=int(os.getenv("QP_NOTIFY_MAX_ATTEMPTS", "5")),
        telegram_bot_token_file=os.getenv("QP_TELEGRAM_BOT_TOKEN_FILE",
                                          "/etc/qp/telegram_bot_token"),
        telegram_chat_id_file=os.getenv("QP_TELEGRAM_CHAT_ID_FILE",
                                        "/etc/qp/telegram_chat_id"),
        telegram_api_base=os.getenv("QP_TELEGRAM_API_BASE", "https://api.telegram.org"),
        telegram_proxy=os.getenv("QP_TELEGRAM_PROXY") or None,
        telegram_timeout_sec=float(os.getenv("QP_TELEGRAM_TIMEOUT_SEC", "10")),
        approved_signals_hmac_file=os.getenv("QP_APPROVED_SIGNALS_HMAC_FILE",
                                             "/etc/qp/approved_signals_hmac"),
        approved_signals_max_skew_sec=int(os.getenv("QP_APPROVED_SIGNALS_MAX_SKEW_SEC", "300")),
        execution_digest_hmac_file=os.getenv("QP_EXECUTION_DIGEST_HMAC_FILE",
                                             "/etc/qp/dedirock_digest_hmac"),
        execution_digest_output_dir=os.getenv("QP_EXECUTION_DIGEST_DIR",
                                              "/var/lib/qp/signals/digests"),
        record_chain=os.getenv("QP_RECORD_CHAIN", "0").strip().lower() in {"1", "true", "yes", "on"},
    )
