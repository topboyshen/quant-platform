#!/usr/bin/env python3
"""
akshare 每日特色数据采集工具
用途：收集收盘后的龙虎榜、板块行情等特色数据，存入数据库或推送报告

建议 crontab 配置：
    0 17 * * 1-5 cd /home/ubuntu/trading_radar && venv/bin/python tools/akshare_daily.py --all >> logs/akshare_daily.log 2>&1

参数：
    --lhb       采集今日龙虎榜
    --sector    采集行业板块行情
    --all       采集所有
    --push      通过 Telegram 推送摘要报告
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# 添加项目根目录到路径
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from radar.data.akshare_adapter import AKShareDataFetcher

# 配置
DATA_DIR = PROJECT_ROOT / "data" / "akshare"
DB_PATH = DATA_DIR / "akshare_data.db"
LOG_FILE = PROJECT_ROOT / "logs" / "akshare_daily.log"
CONFIG_PATH = PROJECT_ROOT / "config.json"

# 确保目录存在
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding='utf-8')
    ]
)
logger = logging.getLogger('akshare_daily')


def init_database():
    """初始化 SQLite 数据库"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 龙虎榜表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS lhb_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            close REAL,
            change_pct REAL,
            net_buy REAL,
            buy_amount REAL,
            sell_amount REAL,
            reason TEXT,
            turnover REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(date, code)
        )
    ''')

    # 板块行情表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sector_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            name TEXT NOT NULL,
            code TEXT,
            price REAL,
            change_pct REAL,
            change REAL,
            turnover REAL,
            up_count INTEGER,
            down_count INTEGER,
            lead_stock TEXT,
            lead_change_pct REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(date, name)
        )
    ''')

    # 存储结果表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS collection_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            type TEXT NOT NULL,
            status TEXT,
            count INTEGER,
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()
    conn.close()
    logger.info(f"数据库初始化完成: {DB_PATH}")


def log_collection(date_str: str, col_type: str, status: str, count: int = 0, message: str = ""):
    """记录采集日志"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO collection_log (date, type, status, count, message)
        VALUES (?, ?, ?, ?, ?)
    ''', (date_str, col_type, status, count, message))
    conn.commit()
    conn.close()


def collect_lhb(fetcher: AKShareDataFetcher, date_str: str) -> dict:
    """采集龙虎榜数据"""
    logger.info(f"=== 开始采集龙虎榜 {date_str} ===")

    data = fetcher.get_lhb_data(date_str)
    if not data:
        log_collection(date_str, 'lhb', 'failed', 0, '获取失败或无数据')
        return {'success': False, 'count': 0}

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    inserted = 0

    for stock in data['stocks']:
        try:
            cursor.execute('''
                INSERT OR REPLACE INTO lhb_data
                (date, code, name, close, change_pct, net_buy, buy_amount, sell_amount, reason, turnover)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                date_str,
                stock['code'],
                stock['name'],
                stock['close'],
                stock['change_pct'],
                stock['net_buy'],
                stock['buy_amount'],
                stock['sell_amount'],
                stock['reason'],
                stock['turnover']
            ))
            inserted += 1
        except Exception as e:
            logger.warning(f"插入龙虎榜数据失败: {stock.get('code')} - {e}")

    conn.commit()
    conn.close()

    log_collection(date_str, 'lhb', 'success', inserted)
    logger.info(f"=== 龙虎榜采集完成: 共 {inserted} 条 ===")

    return {
        'success': True,
        'count': inserted,
        'top_stocks': data['stocks'][:5]  # 前5只用于报告
    }


def collect_sectors(fetcher: AKShareDataFetcher, date_str: str) -> dict:
    """采集行业板块行情"""
    logger.info(f"=== 开始采集板块行情 {date_str} ===")

    data = fetcher.get_sector_board_data()
    if not data:
        log_collection(date_str, 'sector', 'failed', 0, '获取失败或无数据')
        return {'success': False, 'count': 0}

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    inserted = 0

    for sector in data['sectors']:
        try:
            cursor.execute('''
                INSERT OR REPLACE INTO sector_data
                (date, name, code, price, change_pct, change, turnover,
                 up_count, down_count, lead_stock, lead_change_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                date_str,
                sector['name'],
                sector['code'],
                sector['price'],
                sector['change_pct'],
                sector['change'],
                sector['turnover'],
                sector['up_count'],
                sector['down_count'],
                sector['lead_stock'],
                sector['lead_change_pct']
            ))
            inserted += 1
        except Exception as e:
            logger.warning(f"插入板块数据失败: {sector.get('name')} - {e}")

    conn.commit()
    conn.close()

    log_collection(date_str, 'sector', 'success', inserted)
    logger.info(f"=== 板块行情采集完成: 共 {inserted} 个 ===")

    # 分析热点板块
    top_gainers = [s for s in data['sectors'] if s['change_pct'] > 0][:5]
    top_losers = [s for s in data['sectors'] if s['change_pct'] < 0][-5:]

    return {
        'success': True,
        'count': inserted,
        'top_gainers': top_gainers,
        'top_losers': top_losers[::-1]  # 按跌幅排序
    }


def load_telegram_config() -> dict:
    """加载 Telegram 配置，优先使用环境变量"""
    # 优先从环境变量读取
    bot_token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '')

    # 环境变量已完整则直接使用
    if bot_token and chat_id:
        return {'bot_token': bot_token, 'chat_id': chat_id}

    # 从 config.json 补充缺失的字段
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            config = json.load(f)
        tg = config.get('telegram', {})
        if not bot_token:
            bot_token = tg.get('bot_token', '')
        if not chat_id:
            chat_id = tg.get('chat_id', '')
    except Exception as e:
        logger.warning(f"加载配置失败: {e}")

    return {'bot_token': bot_token, 'chat_id': chat_id}


def push_report(date_str: str, lhb_result: dict, sector_result: dict):
    """通过 Telegram 推送摘要报告"""
    tg_config = load_telegram_config()
    if not tg_config.get('bot_token') or not tg_config.get('chat_id'):
        logger.info("Telegram 配置不完整，跳过推送")
        return

    try:
        import requests

        # 构建报告
        lines = [
            f"\ud83d\udcca <b>akshare 每日特色数据报告</b> ({date_str})",
            ""
        ]

        # 龙虎榜摘要
        if lhb_result['success']:
            lines.append(f"🔥 <b>龙虎榜</b>: 共 {lhb_result['count']} 只上榜")
            if lhb_result.get('top_stocks'):
                lines.append("前5名:")
                for i, s in enumerate(lhb_result['top_stocks'], 1):
                    emoji = "🔺" if s['change_pct'] > 0 else "🔻"
                    lines.append(
                        f"  {i}. {s['name']}({s['code']}) "
                        f"{emoji} {s['change_pct']:+.2f}% | "
                        f"净买: {s['net_buy']/10000:.1f}万"
                    )
            lines.append("")
        else:
            lines.append("⚠️ 龙虎榜采集失败\n")

        # 板块行情摘要
        if sector_result['success']:
            lines.append(f"🏛️ <b>板块行情</b>: 共 {sector_result['count']} 个")
            if sector_result.get('top_gainers'):
                lines.append("涨幅前5:")
                for s in sector_result['top_gainers']:
                    lines.append(
                        f"  🔺 {s['name']} +{s['change_pct']:.2f}% | "
                        f"领涨: {s['lead_stock']}({s['lead_change_pct']:+.2f}%)"
                    )
            if sector_result.get('top_losers'):
                lines.append("🔻 跌幅前5:")
                for s in sector_result['top_losers']:
                    lines.append(
                        f"  🔻 {s['name']} {s['change_pct']:.2f}% | "
                        f"领跌: {s['lead_stock']}({s['lead_change_pct']:+.2f}%)"
                    )
        else:
            lines.append("⚠️ 板块行情采集失败")

        message = "\n".join(lines)

        # 发送
        url = f"https://api.telegram.org/bot{tg_config['bot_token']}/sendMessage"
        payload = {
            'chat_id': tg_config['chat_id'],
            'text': message,
            'parse_mode': 'HTML'
        }
        response = requests.post(url, json=payload, timeout=30)
        if response.status_code == 200:
            logger.info("Telegram 报告推送成功")
        else:
            logger.warning(f"Telegram 推送失败: {response.status_code} {response.text}")

    except Exception as e:
        logger.error(f"Telegram 推送异常: {e}")


def main():
    parser = argparse.ArgumentParser(description='akshare 每日特色数据采集')
    parser.add_argument('--lhb', action='store_true', help='采集龙虎榜')
    parser.add_argument('--sector', action='store_true', help='采集板块行情')
    parser.add_argument('--all', action='store_true', help='采集所有')
    parser.add_argument('--push', action='store_true', help='推送 Telegram 报告')
    parser.add_argument('--date', type=str, default=None,
                        help='指定日期 (YYYYMMDD)，默认今天')
    args = parser.parse_args()

    # 确定日期
    if args.date:
        date_str = args.date
    else:
        date_str = datetime.now().strftime('%Y%m%d')

    # 初始化
    init_database()
    fetcher = AKShareDataFetcher(logger)

    if not fetcher.is_available:
        logger.error("akshare 未可用，退出")
        sys.exit(1)

    lhb_result = {'success': False, 'count': 0}
    sector_result = {'success': False, 'count': 0}

    # 执行采集
    if args.all or args.lhb:
        lhb_result = collect_lhb(fetcher, date_str)

    if args.all or args.sector:
        sector_result = collect_sectors(fetcher, date_str)

    # 推送报告
    if args.push and (lhb_result['success'] or sector_result['success']):
        push_report(date_str, lhb_result, sector_result)

    logger.info("采集任务完成")


if __name__ == '__main__':
    main()
