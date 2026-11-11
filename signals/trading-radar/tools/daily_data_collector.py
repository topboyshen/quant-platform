#!/usr/bin/env python3
"""
每日数据收集器
每天收盘后自动获取监控股票的详细数据

功能:
1. 获取所有监控股票的资金主力流向
2. 获取基本财务指标
3. 保存到 trading_radar/data/daily/ 目录
4. 发送 Telegram 汇总报告

定时运行 (添加到 crontab):
    30 15 * * 1-5 /home/ubuntu/trading_radar/venv/bin/python /home/ubuntu/trading_radar/tools/daily_data_collector.py
"""

import os
import sys
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, os.environ.get(
    "MX_DATA_HOME",
    str(Path.home() / "mx-data" / "mx-data"),
))

# 确保在 trading_radar 目录下运行
os.chdir(os.environ.get("TRADING_RADAR_HOME", str(Path(__file__).parent.parent)))

try:
    from mx_data import query_mx_data
except ImportError:
    print("错误: 无法导入 mx_data")
    sys.exit(1)


def load_config() -> Dict:
    """加载配置"""
    with open('config.json', 'r', encoding='utf-8') as f:
        return json.load(f)


def send_telegram_summary(token: str, chat_id: str, date: str, results: List[Dict]):
    """发送 Telegram 汇总报告"""
    import requests
    
    lines = [f"*\ud83d? 每日数据收集报告 ({date})*\n"]
    
    for result in results:
        name = result['name']
        code = result['code']
        status = result.get('status', '未知')
        
        if status == 'success':
            emoji = "\u2705"
            tables = result.get('tables', 0)
            lines.append(f"{emoji} *{name}* ({code}) - {tables} 个数据表")
        else:
            emoji = "\u274c"
            error = result.get('error', '未知错误')
            lines.append(f"{emoji} *{name}* ({code}) - {error}")
    
    message = "\n".join(lines)
    
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown"
        }, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f"Telegram 发送失败: {e}")
        return False


def collect_stock_data(name: str, code: str, output_base: str) -> Dict:
    """收集单只股票的详细数据"""
    result = {
        'name': name,
        'code': code,
        'timestamp': datetime.now().isoformat(),
        'queries': []
    }
    
    queries = [
        (f"{name}资金主力流向", "资金流向"),
        (f"{name}股东人数量", "股东情况"),
        (f"{name}市盈率市净率", "估值指标"),
    ]
    
    for query, category in queries:
        try:
            output_dir = f"{output_base}/{code}/{category}"
            os.makedirs(output_dir, exist_ok=True)
            
            print(f"  \ud83d? 查询: {query}")
            data = query_mx_data(query, output_dir)
            
            result['queries'].append({
                'category': category,
                'query': query,
                'status': 'success',
                'tables': len(data.get('data', {}).get('dataTableDTOList', []))
            })
            
            time.sleep(1)  # 避免请求过快
            
        except Exception as e:
            print(f"  \u274c 查询失败: {e}")
            result['queries'].append({
                'category': category,
                'query': query,
                'status': 'failed',
                'error': str(e)
            })
    
    result['status'] = 'success' if all(q['status'] == 'success' for q in result['queries']) else 'partial'
    return result


def main():
    print(f"\ud83d? 每日数据收集器启动 - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    
    # 加载配置
    config = load_config()
    targets = config.get('targets', [])
    telegram = config.get('telegram', {})
    
    if not targets:
        print("⚠️ 没有配置监控目标")
        return
    
    # 创建输出目录
    today = datetime.now().strftime('%Y%m%d')
    output_base = f"data/daily/{today}"
    os.makedirs(output_base, exist_ok=True)
    
    print(f"\ud83d? 数据将保存到: {output_base}")
    print(f"\ud83d? 共 {len(targets)} 只股票需要收集\n")
    
    # 收集数据
    results = []
    for target in targets:
        name = target['name']
        code = target['code']
        
        print(f"\ud83d? 收集 {name} ({code}) 的数据...")
        result = collect_stock_data(name, code, output_base)
        results.append(result)
        print()
    
    # 发送汇总报告
    if telegram.get('bot_token') and telegram.get('chat_id'):
        print("📤 发送 Telegram 汇总报告...")
        send_telegram_summary(
            telegram['bot_token'],
            telegram['chat_id'],
            today,
            results
        )
    
    print(f"\u2705 数据收集完成 - 保存位置: {output_base}")
    
    # 清理30天以前的旧数据
    clean_old_data("data/daily", days=30)


def clean_old_data(base_dir: str, days: int = 30):
    """清理旧数据"""
    import shutil
    from datetime import datetime, timedelta
    
    cutoff = datetime.now() - timedelta(days=days)
    
    if not os.path.exists(base_dir):
        return
    
    for item in os.listdir(base_dir):
        try:
            item_date = datetime.strptime(item, '%Y%m%d')
            if item_date < cutoff:
                item_path = os.path.join(base_dir, item)
                shutil.rmtree(item_path)
                print(f"🧹 已清理旧数据: {item}")
        except ValueError:
            continue  # 不是日期格式的目录跳过


if __name__ == "__main__":
    main()
