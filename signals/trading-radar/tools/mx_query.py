#!/usr/bin/env python3
"""
妙想金融数据快速查询工具
与 trading_radar 集成，支持 Telegram 通知

用法:
    python tools/mx_query.py "东方财富资金流向"
    python tools/mx_query.py "茅台近一年净利润"
    python tools/mx_query.py --config config.json "康比特公司简介"
"""

import os
import sys
import json
import subprocess
import requests
from pathlib import Path
from typing import Optional, Dict, Any

MX_DATA_PATH = os.environ.get(
    "MX_SCRIPT",
    str(Path.home() / "mx-data" / "mx-data" / "mx_data.py"),
)
VENV_PYTHON = os.environ.get(
    "TRADING_RADAR_VENV_PYTHON",
    str(Path(__file__).parent.parent / "venv" / "bin" / "python"),
)


def load_config(config_path: str = "config.json") -> Dict:
    """加载配置"""
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def run_mx_query(query: str, output_dir: str) -> Dict:
    """运行 mx_data.py 查询"""
    os.makedirs(output_dir, exist_ok=True)
    
    result = subprocess.run(
        [VENV_PYTHON, MX_DATA_PATH, query, output_dir],
        capture_output=True,
        text=True,
        timeout=60
    )
    
    # 解析结果
    output = result.stdout
    
    # 尝试从输出中提取信息
    import re
    
    # 查找 Excel 文件路径
    excel_match = re.search(r'✅ Excel 文件: (.+)', output)
    json_match = re.search(r'📄 原始JSON: (.+)', output)
    
    # 读取原始 JSON 数据
    json_data = {}
    if json_match:
        json_path = json_match.group(1).strip()
        if os.path.exists(json_path):
            with open(json_path, 'r', encoding='utf-8') as f:
                json_data = json.load(f)
    
    return {
        'success': result.returncode == 0,
        'stdout': output,
        'stderr': result.stderr,
        'excel_path': excel_match.group(1).strip() if excel_match else None,
        'json_path': json_match.group(1).strip() if json_match else None,
        'data': json_data
    }


def send_telegram_message(token: str, chat_id: str, message: str) -> bool:
    """发送 Telegram 消息"""
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f"Telegram 发送失败: {e}")
        return False


def quick_query(query: str, output_dir: Optional[str] = None,
                telegram_token: Optional[str] = None,
                telegram_chat_id: Optional[str] = None) -> Dict:
    """快速查询并返回结果"""
    
    if output_dir is None:
        output_dir = "/tmp/mx_output"
    
    print(f"🔍 查询: {query}")
    result = run_mx_query(query, output_dir)
    
    if result['success']:
        print(result['stdout'])
        
        # 发送 Telegram 通知
        if telegram_token and telegram_chat_id:
            msg = f"*\ud83d? 妙想数据\n\n查询: {query}*\n\n```\n{result['stdout'][:800]}\n```"
            if send_telegram_message(telegram_token, telegram_chat_id, msg):
                print("📤 Telegram 通知已发送")
            else:
                print("⚠️ Telegram 发送失败")
    else:
        print(f"❌ 查询失败: {result['stderr']}")
    
    return result


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='妙想金融数据查询')
    parser.add_argument('query', help='查询内容（支持自然语言）')
    parser.add_argument('--config', default='config.json', help='配置文件路径')
    parser.add_argument('--output', default='/tmp/mx_output', help='输出目录')
    parser.add_argument('--no-telegram', action='store_true', help='不发送 Telegram')
    args = parser.parse_args()
    
    # 加载配置
    config = load_config(args.config)
    tg_config = config.get('telegram', {})
    
    token = tg_config.get('bot_token') if not args.no_telegram else None
    chat_id = tg_config.get('chat_id') if not args.no_telegram else None
    
    # 执行查询
    quick_query(args.query, output_dir=args.output,
                telegram_token=token, telegram_chat_id=chat_id)
