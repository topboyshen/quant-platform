#!/usr/bin/env python3
"""
akshare 快速查询工具
用途：命令行快速查询股票卖盘、板块行情、龙虎榜等

使用：
    python tools/ak_query.py --bid-ask 000001    # 查询卖盘
    python tools/ak_query.py --sector            # 查询板块行情
    python tools/ak_query.py --lhb 20250421      # 查询龙虎榜
    python tools/ak_query.py --monitor 000001    # 持续监控某股卖盘
"""

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from radar.data.akshare_adapter import AKShareDataFetcher
import logging

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger('ak_query')


def format_bid_ask(data: dict) -> str:
    """格式化卖盘数据为表格"""
    if not data:
        return "获取失败"

    lines = []
    lines.append(f"\n╔{'═'*60}╗")
    lines.append(f"║ {data['name']}({data['code']}) 实时行情 {' '*32}║")
    lines.append(f"╠{'═'*60}╣")
    lines.append(f"║ 最新价: {data['price']:<10.2f} 涨跌幅: {data.get('change_pct', 0):>+.2f}%{' '*18}║")
    lines.append(f"║ 今开: {data['open']:<10.2f} 昨收: {data.get('prev_close', 0):<10.2f} 最高: {data['high']:<8.2f} 最低: {data['low']:<8.2f}║")
    lines.append(f"║ 成交量: {data.get('volume', 0)/10000:>10.2f}万手 换手率: {data.get('turnover', 0):>6.2f}%{' '*18}║")
    lines.append(f"╠{'═'*60}╣")

    bid_ask = data.get('bid_ask', {})
    if bid_ask:
        lines.append(f"║ {'卖盘':^58}║")
        for i in range(5, 0, -1):
            price = bid_ask.get(f'sell_{i}', 0)
            vol = bid_ask.get(f'sell_{i}_vol', 0)
            lines.append(f"║  卖{i}: {price:<8.2f} {vol/10000:>10.2f}万股{' '*26}║")
        lines.append(f"║ {'-'*56}║")
        for i in range(1, 6):
            price = bid_ask.get(f'buy_{i}', 0)
            vol = bid_ask.get(f'buy_{i}_vol', 0)
            lines.append(f"║  买{i}: {price:<8.2f} {vol/10000:>10.2f}万股{' '*26}║")
        lines.append(f"╠{'═'*60}╣")
        lines.append(f"║ 外盘: {data.get('outer_vol', 0)/10000:>10.2f}万手 | 内盘: {data.get('inner_vol', 0)/10000:>10.2f}万手{' '*12}║")

    lines.append(f"╚{'═'*60}╝")
    return "\n".join(lines)


def format_sectors(data: dict) -> str:
    """格式化板块行情"""
    if not data:
        return "获取失败"

    sectors = data.get('sectors', [])
    lines = [f"\n{'排名':<6}{'板块':<14}{'涨跌幅':>10}{'换手率':>10}{'领涨股':<12}{'领涨幅':>10}"]
    lines.append("-" * 72)

    for s in sectors[:20]:
        emoji = "↑" if s['change_pct'] > 0 else "↓" if s['change_pct'] < 0 else "-"
        lines.append(
            f"{s['rank']:<6}{s['name']:<14}{emoji}{s['change_pct']:>+8.2f}%"
            f"{s['turnover']:>9.2f}%  {s['lead_stock']:<12}{s['lead_change_pct']:>+9.2f}%"
        )

    return "\n".join(lines)


def monitor_bid_ask(code: str, interval: int = 5, duration: int = 60):
    """持续监控某股卖盘变化"""
    fetcher = AKShareDataFetcher(logger)
    if not fetcher.is_available:
        print("akshare 未可用")
        return

    print(f"开始监控 {code} 卖盘，间隔 {interval}秒，持续 {duration}秒...")
    print("按 Ctrl+C 停止\n")

    prev_data = None
    start_time = time.time()

    try:
        while time.time() - start_time < duration:
            data = fetcher.get_stock_data(code)
            if not data:
                print(f"[{time.strftime('%H:%M:%S')}] 获取失败")
                time.sleep(interval)
                continue

            bid_ask = data.get('bid_ask', {})
            if prev_data:
                prev_bid = prev_data.get('bid_ask', {})
                # 检测大单变化
                changes = []
                if abs(bid_ask.get('buy_1_vol', 0) - prev_bid.get('buy_1_vol', 0)) > 100000:
                    changes.append(f"买一量变: {(bid_ask['buy_1_vol']-prev_bid['buy_1_vol'])/10000:+.1f}万")
                if abs(bid_ask.get('sell_1_vol', 0) - prev_bid.get('sell_1_vol', 0)) > 100000:
                    changes.append(f"卖一量变: {(bid_ask['sell_1_vol']-prev_bid['sell_1_vol'])/10000:+.1f}万")
                if changes:
                    print(f"[{time.strftime('%H:%M:%S')}] {data['price']:.2f} | " + " | ".join(changes))
                else:
                    print(f"[{time.strftime('%H:%M:%S')}] {data['price']:.2f} 价格未变")
            else:
                print(f"[{time.strftime('%H:%M:%S')}] 初始化 | 价格: {data['price']:.2f}")

            prev_data = data
            time.sleep(interval)

    except KeyboardInterrupt:
        print("\n监控已停止")


def main():
    parser = argparse.ArgumentParser(description='akshare 快速查询')
    parser.add_argument('--bid-ask', type=str, metavar='CODE',
                        help='查询股票卖盘 (e.g. 000001)')
    parser.add_argument('--sector', action='store_true',
                        help='查询行业板块行情')
    parser.add_argument('--lhb', type=str, metavar='DATE',
                        help='查询龙虎榜 (YYYYMMDD)')
    parser.add_argument('--monitor', type=str, metavar='CODE',
                        help='持续监控卖盘变化')
    parser.add_argument('--interval', type=int, default=5,
                        help='监控间隔(秒)，默认5')
    parser.add_argument('--duration', type=int, default=60,
                        help='监控时长(秒)，默认60')
    args = parser.parse_args()

    fetcher = AKShareDataFetcher(logger)
    if not fetcher.is_available:
        print("❌ akshare 未可用，请先安装: pip install akshare")
        sys.exit(1)

    if args.bid_ask:
        data = fetcher.get_stock_data(args.bid_ask)
        print(format_bid_ask(data))

    elif args.sector:
        data = fetcher.get_sector_board_data()
        print(format_sectors(data))

    elif args.lhb:
        data = fetcher.get_lhb_data(args.lhb)
        if data:
            print(f"\n龙虎榜 {args.lhb}: 共 {data['count']} 只上榜")
            for i, s in enumerate(data['stocks'][:10], 1):
                print(f"{i}. {s['name']}({s['code']}) "
                      f"{s['change_pct']:+.2f}% | "
                      f"净买: {s['net_buy']/10000:.1f}万 | {s['reason'][:30]}")
        else:
            print("获取失败")

    elif args.monitor:
        monitor_bid_ask(args.monitor, args.interval, args.duration)

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
