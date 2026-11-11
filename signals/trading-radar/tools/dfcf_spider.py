#!/usr/bin/env python3
"""
东方财富网页数据采集器
使用 Camoufox 绕过反爬机机制

功能:
1. 获取股票详情页面数据
2. 获取板块涨跌幅排名
3. 获取资金流向数据
4. 获取新闻和公告

用法:
    python tools/dfcf_spider.py stock 300059    # 获取股票详情
    python tools/dfcf_spider.py sector          # 获取板块数据
    python tools/dfcf_spider.py news 300059     # 获取相关新闻
"""

import os
import sys
import json
import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

os.chdir(Path(__file__).parent.parent)


def ensure_dirs():
    Path("data/spider").mkdir(parents=True, exist_ok=True)


def get_stock_detail(code: str, use_camoufox: bool = True) -> Dict:
    """获取股票详情页面数据"""
    url = f"https://quote.eastmoney.com/concept/sh{code}.html" if code.startswith('6') else f"https://quote.eastmoney.com/concept/sz{code}.html"

    print(f"🔍 获取股票 {code} 详情...")

    if use_camoufox:
        try:
            from camoufox.sync_api import Camoufox

            with Camoufox(headless=True) as browser:
                page = browser.new_page()
                page.goto(url, wait_until='networkidle', timeout=30000)

                # 等待页面加载
                page.wait_for_timeout(3000)

                try:
                    page.wait_for_selector('.quote-title, .price, .stock-name', timeout=10000)
                except TimeoutError:
                    pass

                # 获取页面标题
                title = page.title()

                # 获取股票名称
                try:
                    stock_name = page.locator('.quote-title .name').text_content(timeout=5000)
                except (TimeoutError, Exception):
                    stock_name = "未知"

                # 获取当前价格
                try:
                    price = page.locator('.quote-price .price').text_content(timeout=5000)
                except (TimeoutError, Exception):
                    price = None

                # 获取涨跌幅
                try:
                    change = page.locator('.quote-price .change').text_content(timeout=5000)
                except (TimeoutError, Exception):
                    change = None

                # 截图保存
                screenshot_path = f"data/spider/stock_{code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                page.screenshot(path=screenshot_path, full_page=False)

                data = {
                    'code': code,
                    'name': stock_name,
                    'price': price,
                    'change': change,
                    'title': title,
                    'url': url,
                    'screenshot': screenshot_path,
                    'source': '东方财富',
                    'timestamp': datetime.now().isoformat()
                }

                # 保存数据
                ensure_dirs()
                with open(f"data/spider/stock_{code}.json", 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)

                return data

        except (ImportError, RuntimeError) as e:
            print(f"⚠️ Camoufox 不可用: {e}")
            use_camoufox = False

    # 备用方案：使用 akshare
    if not use_camoufox:
        try:
            import akshare as ak
            df = ak.stock_zh_a_spot_em()
            stock_info = df[df['代码'] == code]
            if not stock_info.empty:
                info = stock_info.iloc[0]
                return {
                    'code': code,
                    'name': info.get('名称', '未知'),
                    'price': str(info.get('最新价', '')),
                    'change': str(info.get('涨跌幅', '')),
                    'url': url,
                    'source': 'akshare',
                    'timestamp': datetime.now().isoformat()
                }
        except (ImportError, Exception) as e:
            print(f"⚠️ akshare 获取失败: {e}")

    return {'code': code, 'error': '无法获取数据', 'url': url}


def get_sector_data() -> List[Dict]:
    """获取板块涨跌幅数据"""
    try:
        import akshare as ak
        # 东方财富板块接口已失效，使用同花顺行业资金流向接口
        df = ak.stock_fund_flow_industry()
        sectors = []
        for _, row in df.head(20).iterrows():
            sectors.append({
                'name': row.get('行业', ''),
                'change': row.get('行业-涨跌幅', ''),
                'volume': row.get('公司家数', ''),
                'leader': row.get('领涨股', '')
            })
        return sectors
    except (ImportError, Exception) as e:
        print(f"⚠️ 获取板块数据失败: {e}")
        return []


def get_news_data(code: str, limit: int = 10) -> List[Dict]:
    """获取股票相关新闻"""
    try:
        import akshare as ak
        df = ak.stock_news_em(symbol=code)
        news = []
        for _, row in df.head(limit).iterrows():
            news.append({
                'title': row.get('标题', ''),
                'content': row.get('内容', '')[:200],
                'time': row.get('发布时间', ''),
                'url': row.get('链接', '')
            })
        return news
    except (ImportError, Exception) as e:
        print(f"⚠️ 获取新闻失败: {e}")
        return []


def get_money_flow(code: str) -> Dict:
    """获取资金流向数据"""
    try:
        import akshare as ak
        df = ak.stock_individual_money_flow_stockid(symbol=code)
        if not df.empty:
            latest = df.iloc[0]
            return {
                'main_in': latest.get('主力净流入', ''),
                'main_out': latest.get('主力净流出', ''),
                'retail_in': latest.get('散户净流入', ''),
                'retail_out': latest.get('散户净流出', ''),
                'timestamp': datetime.now().isoformat()
            }
    except (ImportError, Exception) as e:
        print(f"⚠️ 获取资金流向失败: {e}")
    return {}


def main():
    parser = argparse.ArgumentParser(description='东方财富数据采集器')
    parser.add_argument('command', choices=['stock', 'sector', 'news', 'money'])
    parser.add_argument('code', nargs='?', help='股票代码')
    parser.add_argument('--no-camoufox', action='store_true', help='不使用 Camoufox')

    args = parser.parse_args()

    if args.command == 'stock':
        if not args.code:
            print("❌ 请提供股票代码")
            sys.exit(1)
        result = get_stock_detail(args.code, use_camoufox=not args.no_camoufox)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == 'sector':
        sectors = get_sector_data()
        print(json.dumps(sectors, ensure_ascii=False, indent=2))

    elif args.command == 'news':
        if not args.code:
            print("❌ 请提供股票代码")
            sys.exit(1)
        news = get_news_data(args.code)
        print(json.dumps(news, ensure_ascii=False, indent=2))

    elif args.command == 'money':
        if not args.code:
            print("❌ 请提供股票代码")
            sys.exit(1)
        flow = get_money_flow(args.code)
        print(json.dumps(flow, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
