#!/usr/bin/env python3
"""
ML模型自动训练脚本
收盘后运行，为所有监控股票训练模型
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

import pandas as pd
import requests

# 导入妙想客户端
from mx_client import get_stock_history_mx

# 尝试导入 akshare 作为备用
try:
    import akshare as ak
    AKSHARE_AVAILABLE = True
except ImportError:
    AKSHARE_AVAILABLE = False

# 设置日志
# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('ml_training.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from radar.ml.predictor import MLPredictor

# 必须晚于 logging.basicConfig（在本文件顶部）以保证 unified_history 模块
# 内部 getLogger 走相同 handler；属于刻意的运行顺序依赖，不是失误
from radar.data.unified_history import log_history_lineage, normalize_history_df  # noqa: E402


def _ml_history_fallback_enabled() -> bool:
    """为 True 时在东财日 K 失败后再尝试妙想 / akshare / baostock。"""
    return os.getenv("RADAR_ML_HISTORY_FALLBACK", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def send_telegram_notification(token, chat_id, message):
    """发送Telegram通知"""
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown"
        }
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Telegram通知失败: {e}")


def get_stock_history_mkt(code, days=150):
    """
    获取股票历史日 K。

    默认与回测一致：``MarketDataFetcher.get_historical_data``（东财 push2his，已标准化 + 血缘）。
    若需在东财失败后再尝试妙想 → akshare → baostock，请设置环境变量
    ``RADAR_ML_HISTORY_FALLBACK=1``。

    Parquet「防洪坝」：设置 ``RADAR_KLINE_PARQUET_PREFER_READ=1`` 时优先读本地
    ``RADAR_KLINE_PARQUET_DIR``（默认 data/kline_parquet）。
    东财路径的自动落盘在 ``MarketDataFetcher.get_historical_data`` 内完成；
    妙想/akshare/baostock 成功时在本函数内落盘。``RADAR_KLINE_PARQUET_AUTO_WRITE=0`` 关闭写盘。
    """
    from radar.data.market_data import MarketDataFetcher
    from radar.data.ohlcv_parquet_store import (
        max_age_days_from_env,
        maybe_persist_fetched_kline,
        prefer_read_enabled,
        try_load_kline_parquet,
    )

    if prefer_read_enabled():
        cached = try_load_kline_parquet(
            code,
            min_rows=max(30, days // 2),
            max_age_days=max_age_days_from_env(),
        )
        if cached is not None:
            log_history_lineage(logger, f"历史K {code} (parquet_cache)", cached)
            return cached

    fetcher = MarketDataFetcher(logger, use_akshare=True)
    try:
        df = fetcher.get_historical_data(code, days=days)
    except Exception as e:
        logger.warning("东财历史K获取异常 %s: %s", code, e)
        df = pd.DataFrame()

    if df is not None and len(df) > 0:
        log_history_lineage(logger, f"历史K {code} (eastmoney)", df)
        return df

    if not _ml_history_fallback_enabled():
        logger.warning(
            "东财历史K为空或不足，且未启用备用源；"
            "训练与回测对齐时请勿开启 fallback。"
            " 若必须启用妙想/akshare/baostock，请设置 RADAR_ML_HISTORY_FALLBACK=1（code=%s）",
            code,
        )
        return None

    logger.info("东财历史K不可用，启用备用链 RADAR_ML_HISTORY_FALLBACK（code=%s）", code)

    stock_name = None
    try:
        from radar.config_loader import load_config
        config = load_config()
        for target in config.get("targets", []):
            if target["code"] == code:
                stock_name = target["name"]
                break
    except Exception:
        # 异常被静默忽略，如需排查请开启调试日志
        pass

    df = get_stock_history_mx(code, stock_name, days)
    if df is not None and len(df) > 0:
        df = normalize_history_df(
            df,
            source="mx_api",
            pipeline="ml_training.get_stock_history_mkt",
            adjustment="unknown",
        )
        log_history_lineage(logger, f"历史K {code} (mx)", df)
        maybe_persist_fetched_kline(code, df, persist_reason="ml_training_mx")
        return df

    if AKSHARE_AVAILABLE:
        logger.info("妙想API失败，使用 akshare 备用...")
        df = _get_stock_history_akshare(code, days)
        if df is not None and len(df) > 0:
            df = normalize_history_df(
                df,
                source="akshare_zh_a_hist",
                pipeline="ml_training.get_stock_history_mkt",
                adjustment="qfq",
            )
            log_history_lineage(logger, f"历史K {code} (akshare)", df)
            maybe_persist_fetched_kline(code, df, persist_reason="ml_training_akshare")
            return df

    logger.info("akshare 也失败，尝试 baostock 第二备用...")
    df = _get_stock_history_baostock(code, days)
    if df is not None and len(df) > 0:
        df = normalize_history_df(
            df,
            source="baostock",
            pipeline="ml_training.get_stock_history_mkt",
            adjustment="qfq",
        )
        log_history_lineage(logger, f"历史K {code} (baostock)", df)
        maybe_persist_fetched_kline(code, df, persist_reason="ml_training_baostock")
        return df

    return None
def _get_stock_history_akshare(code, days=150):
    """akshare 备用方案"""
    try:
        full_code = code.zfill(6)
        start_date = (datetime.now() - timedelta(days=days+30)).strftime('%Y%m%d')
        end_date = datetime.now().strftime('%Y%m%d')

        df = ak.stock_zh_a_hist(
            symbol=full_code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="qfq"
        )

        if df is not None and len(df) > 0:
            df = df.rename(columns={
                '日期': 'date',
                '收盘': 'close',
                '成交量': 'volume'
            })
            df['price'] = df['close'].astype(float)
            df['volume'] = df['volume'].astype(int)
            return df

    except Exception as e:
        logger.error(f"akshare 备用也失败: {e}")
    return None


def _get_stock_history_baostock(code, days=150):
    """baostock 第二备用方案（当 akshare 也失败时）"""
    try:
        import baostock as bs

        # 判断上市场所
        if code.startswith('6'):
            full_code = f'sh.{code}'
        else:
            full_code = f'sz.{code}'

        start_date = (datetime.now() - timedelta(days=days+30)).strftime('%Y-%m-%d')
        end_date = datetime.now().strftime('%Y-%m-%d')

        lg = bs.login()
        if lg.error_code != '0':
            logger.warning(f"baostock 登录失败: {lg.error_msg}")
            return None

        rs = bs.query_history_k_data_plus(
            full_code,
            'date,open,high,low,close,volume',
            start_date=start_date,
            end_date=end_date,
            frequency='d',
            adjustflag='3'  # 复权
        )

        data_list = []
        while (rs.error_code == '0') & rs.next():
            data_list.append(rs.get_row_data())

        bs.logout()

        if not data_list:
            return None

        df = pd.DataFrame(data_list, columns=rs.fields)
        df['price'] = df['close'].astype(float)
        df['volume'] = df['volume'].astype(float)
        df['date'] = df['date']

        logger.info(f"baostock 获取成功: {len(df)} 条")
        return df

    except Exception as e:
        logger.error(f"baostock 备用也失败: {e}")
    return None


def _existing_model_entry(code: str, direction: str) -> Optional[dict]:
    """Return manifest entry when a usable model artifact already exists."""
    try:
        from radar.model_manifest import load_manifest
    except Exception as e:
        logger.debug("读取模型 manifest 依赖失败 %s_%s: %s", code, direction, e)
        return None

    try:
        manifest = load_manifest()
        entry = manifest.get("models", {}).get(f"{code}_{direction}")
        if not isinstance(entry, dict):
            return None

        artifacts = entry.get("artifacts", {})
        model_path = artifacts.get("model") or os.path.join("models", f"{code}_{direction}_model.pkl")
        scaler_path = artifacts.get("scaler") or os.path.join("models", f"{code}_{direction}_scaler.pkl")
        if os.path.isfile(model_path) and os.path.isfile(scaler_path):
            return entry
        logger.warning(
            "已有模型 manifest 记录但产物缺失 %s_%s: model=%s scaler=%s",
            code,
            direction,
            model_path,
            scaler_path,
        )
    except Exception as e:
        logger.warning("检查已有模型失败 %s_%s: %s", code, direction, e)
    return None


def _retained_existing_model_result(entry: dict, reason: str) -> dict:
    metrics = entry.get("metrics", {}) if isinstance(entry, dict) else {}
    return {
        "success": True,
        "retained_existing_model": True,
        "reason": reason,
        "accuracy": float(metrics.get("accuracy", 0) or 0),
        "precision": float(metrics.get("precision", 0) or 0),
        "recall": float(metrics.get("recall", 0) or 0),
        "samples": int(metrics.get("samples", 0) or 0),
        "positive_samples": int(metrics.get("positive_samples", 0) or 0),
        "trained_at_utc": entry.get("trained_at_utc"),
    }


def _train_single(
    predictor: MLPredictor,
    target: dict,
    max_retries: int = 3,
) -> tuple[bool, dict, Optional[str]]:
    """训练单只股票模型，返回 (success, result_dict, error_reason)。"""
    code = target["code"]
    trigger_price = target["trigger_price"]
    direction = target["direction"]

    for retry in range(max_retries):
        try:
            df = get_stock_history_mkt(code, days=150)
            if df is None or len(df) < 50:
                logger.warning(f"   数据不足 ({len(df) if df is not None else 0}条)")
                if retry < max_retries - 1:
                    time.sleep(2)
                    continue
                return False, {}, "insufficient_data"

            logger.info(f"   获取到 {len(df)} 条数据")
            result = predictor.train(
                code=code,
                direction=direction,
                df=df,
                trigger_price=trigger_price,
                prediction_window=5,
            )

            if result.get("success"):
                logger.info("   ✅ 训练成功!")
                logger.info(f"      准确率: {result['accuracy']:.1%}")
                logger.info(f"      精确率: {result['precision']:.1%}")
                logger.info(f"      召回率: {result['recall']:.1%}")
                return True, result, None
            else:
                reason = result.get("reason")
                if reason == "no_positive_samples":
                    existing = _existing_model_entry(code, direction)
                    if existing is not None:
                        logger.warning(
                            "   ⚠️ 当前窗口没有正样本，保留已有模型（trained_at=%s）",
                            existing.get("trained_at_utc", "unknown"),
                        )
                        return True, _retained_existing_model_result(existing, reason), None
                    logger.error("   ❌ 当前窗口没有正样本，且没有可保留的已有模型")
                    return False, {}, reason

                logger.error(f"   ❌ 训练失败: {reason}")
                if retry < max_retries - 1:
                    time.sleep(2)
                    continue
                return False, {}, reason

        except Exception as e:
            logger.error(f"   ❌ 错误: {e}")
            if retry < max_retries - 1:
                logger.info(f"   重试中... ({retry+1}/{max_retries})")
                time.sleep(2)
                continue
            return False, {}, str(e)

    return False, {}, "max_retries_exceeded"


def train_all_models():
    """训练所有模型"""
    from radar.config_loader import load_config
    from radar.ml.predictor import MLPredictor

    config = load_config()
    targets = config["targets"]
    bot_token = config["telegram"]["bot_token"]
    chat_id = config["telegram"]["chat_id"]

    ml_targets = [t for t in targets if t.get("ml_enabled", True)]
    skipped_count = len(targets) - len(ml_targets)

    predictor = MLPredictor(model_dir="models", logger=logger)

    logger.info("=" * 70)
    logger.info(f"🤖 ML模型自动训练 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 70)
    logger.info(f"共 {len(targets)} 只股票，其中 {len(ml_targets)} 只启用ML训练")
    if skipped_count > 0:
        logger.info(f"⏸️ {skipped_count} 只股票ML预测已禁用（除权/特殊情况）")

    trained: list[dict] = []
    retained: list[dict] = []
    failed: list[dict] = []

    for i, target in enumerate(ml_targets, 1):
        code = target["code"]
        name = target["name"]

        if not target.get("ml_enabled", True):
            logger.info(f"\n[{i}/{len(targets)}] {name} ({code}) - ⏸️ ML预测已禁用，跳过训练")
            continue

        logger.info(f"\n[{i}/{len(targets)}] 训练 {name} ({code})...")
        ok, result, reason = _train_single(predictor, target)

        if ok:
            target_result = {
                "name": name,
                "code": code,
                "accuracy": result["accuracy"],
                "precision": result["precision"],
                "recall": result["recall"],
                "samples": result["samples"],
                "reason": result.get("reason"),
                "trained_at_utc": result.get("trained_at_utc"),
            }
            if result.get("retained_existing_model"):
                retained.append(target_result)
            else:
                trained.append(target_result)
        else:
            failed.append({"name": name, "reason": reason})

        time.sleep(1)

    report = generate_report(trained, failed, skipped_count, retained=retained)
    logger.info("\n" + report)
    send_telegram_notification(bot_token, chat_id, report)

    logger.info("\n" + "=" * 70)
    logger.info("✅ 训练任务完成")
    logger.info("=" * 70)


def generate_report(trained, failed, skipped_count=0, retained=None):
    """生成训练报告"""
    retained = retained or []
    report = f"""🤖 *ML模型训练报告*

📅 日期: {datetime.now().strftime('%Y-%m-%d %H:%M')}

📊 训练结果:
✅ 新训练成功: {len(trained)} 只
"""

    for t in trained:
        report += f"   • {t['name']}: 准确率 {t['accuracy']:.1%}, 样本 {t['samples']}\n"

    if retained:
        report += f"\n🟡 保留已有模型: {len(retained)} 只\n"
        for t in retained:
            trained_at = t.get("trained_at_utc") or "unknown"
            report += (
                f"   • {t['name']}: 当前窗口无正样本，保留 {trained_at} 模型"
                f"（准确率 {t['accuracy']:.1%}, 样本 {t['samples']}）\n"
            )

    if failed:
        report += f"\n❌ 失败: {len(failed)} 只\n"
        for f in failed:
            report += f"   • {f['name']}: {f['reason']}\n"

    if skipped_count > 0:
        report += f"\n⏸️ 跳过: {skipped_count} 只（ML预测已禁用）\n"
    report += "\n📈 可用模型已更新/保留，明日开盘生效"

    return report


if __name__ == "__main__":
    train_all_models()
