#!/usr/bin/env python3
"""
Auto Trigger Price Updater for trading_radar
每天收盘后自动根据波动率重新计算触发价
用法: python3 auto_trigger_updater.py [--dry-run]
"""

import glob
import json
import os
import sqlite3
import sys
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

# 添加项目路径
sys.path.insert(0, '/home/ubuntu/trading_radar')

import akshare as ak

CONFIG_PATH = '/home/ubuntu/trading_radar/config.json'
DB_PATH = '/home/ubuntu/trading_radar/battlefield_state.db'


def get_positions():
    """获取持仓信息"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT code, shares, cost FROM position_state WHERE shares > 0")
    positions = {row[0]: {'shares': row[1], 'cost': row[2]} for row in c.fetchall()}
    conn.close()
    return positions


def get_volatility(code, days=10):
    """获取近N日平均振幅（使用同花顺数据源）"""
    import time, requests, re, json
    
    # 同花顺数据源: d.10jqka.com.cn
    # 股票前缀: 上海sh/深圳sz/北京bj
    if code.startswith('8') or code.startswith('9'):
        prefix = 'bj'
    elif code.startswith('6'):
        prefix = 'sh'
    else:
        prefix = 'sz'
    
    url = f"http://d.10jqka.com.cn/v4/line/hs_{code}/01/last.js"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=15)
            r.raise_for_status()
            data = r.text
            
            # 解析同花顺格式: quotebridge_v4_line_xxx({"data":"..."})
            match = re.search(r'\(({.*})\)', data, re.DOTALL)
            if not match:
                return None, None
            
            j = json.loads(match.group(1))
            
            # data字段格式: date,open,high,low,close,volume,amount,amplitude...;...
            lines = j.get('data', '').split(';')
            if len(lines) < 3:
                return None, None
            
            # 取最近days天计算平均振幅
            amps = []
            prices = []
            for line in lines[-days:]:
                parts = line.split(',')
                if len(parts) >= 5:
                    high = float(parts[2])
                    low = float(parts[3])
                    close = float(parts[4])
                    if close > 0:
                        amp = (high - low) / close * 100
                        amps.append(amp)
                        prices.append(close)
            
            if not amps:
                return None, None
            
            avg_amp = sum(amps) / len(amps)
            latest_close = prices[-1] if prices else 0
            
            # 限制振幅范围
            if avg_amp < 0.5:
                avg_amp = 2.0
            elif avg_amp > 8.0:
                avg_amp = 8.0
            
            return avg_amp, latest_close
            
        except Exception as e:
            if attempt < 2:
                print(f"  ⚠️ 获取 {code} 失败(重试{attempt+1}): {e}")
                time.sleep(2)
            else:
                print(f"  ⚠️ 获取 {code} 数据失败: {e}")
                return None, None


def calculate_trigger(price, amp, direction, has_position=False, cost=0):
    """根据波动率计算触发价"""
    if amp is None or price is None:
        return None
    
    # 波动率等级
    if amp < 2.5:
        base_pct = 0.07
        n = 1.5
    elif amp < 4.0:
        base_pct = 0.08
        n = 2.0
    else:
        base_pct = 0.12
        n = 2.5
    
    # 公式计算距离
    formula_dist = max(base_pct, amp * n / 100)
    
    # 修正规则
    if has_position and direction == 'below':
        # 有持仓below: 触发价 >= 成本 * 88%, 且距离 >= 7%
        min_from_cost = cost * 0.88
        formula_trigger = price * (1 - formula_dist)
        new_trigger = min(formula_trigger, min_from_cost)
        # 确保距离至少7%
        if price - new_trigger < price * 0.07:
            new_trigger = price * 0.93
        # 确保不超过成本线（不能比成本还高）
        if new_trigger > cost:
            new_trigger = cost * 0.95  # 成本线下方5%
        return round(new_trigger, 2)
    elif direction == 'below':
        # below: 距离 >= max(6%, 计算值), 且不超过近期低点支撑
        dist = max(0.06, formula_dist)
        # 高价股(>100元)适当收紧，避免距离过远
        if price > 100:
            dist = max(0.05, min(dist, 0.08))  # 5-8%缓冲
        # 中价股(20-100元)标准距离
        elif price > 20:
            dist = max(0.06, min(dist, 0.10))  # 6-10%缓冲
        return round(price * (1 - dist), 2)
    else:  # above
        # above: 低波动股票收紧距离
        dist = max(0.05, formula_dist)
        # 极低波动(<1.5%)的above触发，距离不超过5%
        if amp < 1.5:
            dist = max(0.03, min(dist, 0.05))  # 3-5%缓冲
        # 低波动(1.5-2.5%)的above触发，距离不超过6%
        elif amp < 2.5:
            dist = max(0.04, min(dist, 0.06))  # 4-6%缓冲
        return round(price * (1 + dist), 2)


def update_config(targets, dry_run=False):
    """更新config.json"""
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        config = json.load(f)
    
    # 更新targets
    for t in config['targets']:
        code = t['code']
        if code in targets:
            t['trigger_price'] = targets[code]['new_trigger']
    
    if dry_run:
        print("\n=== 预览模式，不保存 ===")
        return
    
    # 备份原配置
    backup_path = CONFIG_PATH + '.backup.' + datetime.now().strftime('%Y%m%d%H%M%S')
    with open(backup_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    print(f"  配置已备份: {backup_path}")

    # 仅保留最新 BACKUP_RETENTION 份（之前从不清理，会堆到几十份）
    BACKUP_RETENTION = 5
    backups = sorted(glob.glob(CONFIG_PATH + '.backup.*'), reverse=True)
    for stale in backups[BACKUP_RETENTION:]:
        try:
            os.remove(stale)
            print(f"  清理旧备份: {stale}")
        except OSError as e:
            print(f"  清理失败 {stale}: {e}")

    # 写入新配置
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    print(f"  配置已更新: {CONFIG_PATH}")


def restart_service():
    """重启监控服务"""
    result = subprocess.run(
        ['sudo', 'systemctl', 'restart', 'stock-radar.service'],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print("  ✅ stock-radar.service 已重启")
    else:
        print(f"  ❌ 重启失败: {result.stderr}")


def main():
    dry_run = '--dry-run' in sys.argv
    
    print(f"{'='*60}")
    print(f"自动触发价更新 - {'预览模式' if dry_run else '执行模式'}")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")
    
    # 获取持仓
    positions = get_positions()
    print(f"持仓情况: {len(positions)} 只")
    for code, p in positions.items():
        print(f"  {code}: {p['shares']}股, 成本¥{p['cost']:.2f}")
    
    print(f"\n开始计算...\n")
    
    # 读取当前配置
    with open(CONFIG_PATH, 'r') as f:
        config = json.load(f)
    
    results = {}
    for target in config['targets']:
        code = target['code']
        name = target['name']
        old_trigger = target['trigger_price']
        direction = target['direction']
        
        has_pos = code in positions
        cost = positions.get(code, {}).get('cost', 0)
        
        print(f"{name} ({code}) - {direction}:")
        
        # 获取波动率
        amp, price = get_volatility(code)
        if amp is None:
            print(f"  ⚠️ 跳过（数据获取失败）")
            continue
        
        print(f"  当前价: ¥{price:.2f}, 近10日平均振幅: {amp:.2f}%")
        
        # 计算新触发价
        new_trigger = calculate_trigger(price, amp, direction, has_pos, cost)
        if new_trigger is None:
            print(f"  ⚠️ 跳过（计算失败）")
            continue
        
        # 计算距离
        if direction == 'below':
            dist = (new_trigger - price) / price * 100
        else:
            dist = (new_trigger - price) / price * 100
        
        print(f"  原触发价: ¥{old_trigger:.2f} → 新触发价: ¥{new_trigger:.2f} ({dist:+.1f}%)")
        
        results[code] = {
            'name': name,
            'old': old_trigger,
            'new': new_trigger,
            'price': price,
            'amp': amp,
            'direction': direction,
            'new_trigger': new_trigger,
        }
    
    # 更新配置
    if results:
        print(f"\n{'='*60}")
        print("更新配置...")
        update_config(results, dry_run=dry_run)
        
        if not dry_run:
            print(f"\n重启服务...")
            restart_service()
    else:
        print("\n⚠️ 没有可更新的股票")
    
    print(f"\n{'='*60}")
    print("完成")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
