#!/usr/bin/env python3
"""
自动监控工具
定时检查网页变化、股票页面更新等

用法:
    # 手动运行一次检查
    python tools/auto_monitor.py

    # 添加到 crontab 自动运行
    0 */2 * * * /home/ubuntu/trading_radar/venv/bin/python /home/ubuntu/trading_radar/tools/auto_monitor.py
"""

import os
import sys
import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import List, Dict
import requests

os.chdir(Path(__file__).parent.parent)

# 监控配置
MONITOR_CONFIG = {
    'urls': [
        # 监控的网页列表
        'https://quote.eastmoney.com/concept/sh300059.html',  # 东方财富
        'https://quote.eastmoney.com/concept/sz300033.html',  # 同花顺
    ],
    'keywords': [
        # 关键词监控（可从网页内容中检测）
        '重大利好',
        '重大利空',
        '停牌',
        '复牌',
    ]
}


def load_config():
    """加载配置"""
    with open('config.json', 'r', encoding='utf-8') as f:
        return json.load(f)


def send_telegram_message(token: str, chat_id: str, message: str):
    """发送 Telegram 消息"""
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        resp = requests.post(url, json=payload, timeout=15)
        return resp.status_code == 200
    except Exception as e:
        print(f"Telegram 发送失败: {e}")
        return False


def get_page_hash(url: str) -> str:
    """获取网页内容哈希"""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.goto(url, wait_until='networkidle', timeout=20000)
            page.wait_for_timeout(2000)

            # 获取关键元素的内容
            content = page.locator('body').text_content()
            browser.close()

            return hashlib.md5(content.encode()).hexdigest()
    except Exception as e:
        print(f"获取页面哈希失败: {e}")
        return ""


def check_keywords(url: str, keywords: List[str]) -> List[str]:
    """检查页面是否包含关键词"""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.goto(url, wait_until='networkidle', timeout=20000)
            page.wait_for_timeout(2000)

            content = page.locator('body').text_content().lower()
            browser.close()

            found = []
            for keyword in keywords:
                if keyword in content:
                    found.append(keyword)
            return found
    except Exception as e:
        print(f"检查关键词失败: {e}")
        return []


def load_previous_hashes() -> Dict:
    """加载之前的哈希记录"""
    hash_file = Path('data/monitor_hashes.json')
    if hash_file.exists():
        with open(hash_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_hashes(hashes: Dict):
    """保存哈希记录"""
    Path('data').mkdir(exist_ok=True)
    with open('data/monitor_hashes.json', 'w', encoding='utf-8') as f:
        json.dump(hashes, f, ensure_ascii=False, indent=2)


def main():
    print(f"🔍 开始监控检查 - {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    config = load_config()
    tg = config.get('telegram', {})

    previous_hashes = load_previous_hashes()
    current_hashes = {}
    changes = []

    # 检查每个网页
    for url in MONITOR_CONFIG['urls']:
        print(f"  检查: {url}")

        # 获取当前哈希
        current_hash = get_page_hash(url)
        current_hashes[url] = current_hash

        if not current_hash:
            print(f"    ❌ 获取失败")
            continue

        # 检查是否发生变化
        if url in previous_hashes:
            if previous_hashes[url] != current_hash:
                print(f"    🔔 发现变化!")
                changes.append({
                    'url': url,
                    'type': 'content_changed'
                })
            else:
                print(f"    ✅ 无变化")
        else:
            print(f"    📝 新增监控")

        # 检查关键词
        keywords_found = check_keywords(url, MONITOR_CONFIG['keywords'])
        if keywords_found:
            print(f"    ⚠️ 发现关键词: {', '.join(keywords_found)}")
            changes.append({
                'url': url,
                'type': 'keyword_found',
                'keywords': keywords_found
            })

    # 保存当前哈希
    save_hashes(current_hashes)

    # 发送告警
    if changes and tg.get('bot_token') and tg.get('chat_id'):
        message_lines = [f"🔔 *网页监控告警* ({datetime.now().strftime('%H:%M')})\n"]

        for change in changes:
            if change['type'] == 'content_changed':
                message_lines.append(f"• 页面发生变化: {change['url']}")
            elif change['type'] == 'keyword_found':
                message_lines.append(f"• 发现关键词 `{', '.join(change['keywords'])}`: {change['url']}")

        message = '\n'.join(message_lines)
        if send_telegram_message(tg['bot_token'], tg['chat_id'], message):
            print("📤 告警已发送")
        else:
            print("❌ 告警发送失败")
    elif changes:
        print("⚠️ 发现变化但未配置 Telegram")
    else:
        print("✅ 所有页面正常，无变化")

    print(f"✅ 监控检查完成 - {datetime.now().strftime('%H:%M')}")


if __name__ == "__main__":
    main()
