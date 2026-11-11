"""
akshare 增值数据模块
主要提供资金流向、市场情绪等东方财富API不直接支持的数据
"""

import akshare as ak
import pandas as pd
from datetime import datetime, timedelta
import threading
import requests

# 全局缓存
_cache = {}
_cache_lock = threading.Lock()
_cache_ttl = 30  # 秒


def _get_cache(key: str) -> dict | None:
    """获取缓存"""
    with _cache_lock:
        if key in _cache:
            data, expire_at = _cache[key]
            if datetime.now() < expire_at:
                return data
            else:
                del _cache[key]
    return None


def _set_cache(key: str, data: dict, ttl: int = _cache_ttl) -> None:
    """设置缓存"""
    with _cache_lock:
        _cache[key] = (data, datetime.now() + timedelta(seconds=ttl))


def get_akshare_prices(codes: list[str] | None = None) -> dict:
    """
    通过 akshare 获取 A股实时行情（作为备用数据源）
    注意：akshare 的 stock_zh_a_spot_em 获取全市场较慢，
    此处使用东方财富备用 endpoint，速度更快
    """
    if not codes:
        return {}

    codes = [str(c).strip() for c in codes]
    cache_key = f"prices_{'_'.join(sorted(codes))}"
    cached = _get_cache(cache_key)
    if cached:
        return cached

    prices = {}
    for code in codes:
        try:
            secid = f"0.{code}" if code.startswith(('0', '3')) else f"1.{code}"
            url = 'http://push2.eastmoney.com/api/qt/stock/get'
            params = {
                'ut': 'bd1d9ddb04089700cf9c27f6f7426281',
                'fltt': 2,
                'invt': 2,
                'secid': secid,
                'fields': 'f43,f44,f45,f46,f47,f48,f57,f58,f60,f62,f63,f64,f66,f69,f70,f71,f72,f170'
            }
            resp = requests.get(url, params=params, timeout=8)
            data = resp.json()
            d = data.get('data', {})
            if d:
                prices[code] = {
                    'name': d.get('f58', ''),
                    'price': d.get('f43', 0),
                    'change_pct': d.get('f170', 0),
                    'open': d.get('f46', 0),
                    'high': d.get('f44', 0),
                    'low': d.get('f45', 0),
                    'volume': d.get('f47', 0),
                    'amount': d.get('f48', 0),
                    'prev_close': d.get('f60', 0),
                    'source': 'akshare_backup',
                }
        except Exception as e:
            prices[code] = {'error': str(e)}

    _set_cache(cache_key, prices, ttl=15)
    return prices


def get_capital_flow(code: str) -> dict:
    """
    获取个股资金流向
    code: 股票代码，如 '300059'
    返回: dict, 包含主力资金流向、散户资金流向等
    """
    code_str = str(code).strip()
    cache_key = f"flow_{code_str}"
    cached = _get_cache(cache_key)
    if cached:
        return cached

    result = {
        'code': code_str,
        'timestamp': datetime.now().isoformat(),
        'summary': {}
    }

    try:
        market = 'sz' if code_str.startswith(('0', '3')) else 'sh'
        df = ak.stock_individual_fund_flow(stock=code_str, market=market)
        if not df.empty:
            latest = df.iloc[0]
            result['summary'] = {
                'main_inflow': float(latest.get('主力净流入-净额', 0)) if pd.notna(latest.get('主力净流入-净额')) else 0,
                'main_inflow_pct': float(latest.get('主力净流入-净占比', 0)) if pd.notna(latest.get('主力净流入-净占比')) else 0,
                'super_inflow': float(latest.get('超大单净流入-净额', 0)) if pd.notna(latest.get('超大单净流入-净额')) else 0,
                'super_inflow_pct': float(latest.get('超大单净流入-净占比', 0)) if pd.notna(latest.get('超大单净流入-净占比')) else 0,
                'large_inflow': float(latest.get('大单净流入-净额', 0)) if pd.notna(latest.get('大单净流入-净额')) else 0,
                'large_inflow_pct': float(latest.get('大单净流入-净占比', 0)) if pd.notna(latest.get('大单净流入-净占比')) else 0,
                'medium_inflow': float(latest.get('中单净流入-净额', 0)) if pd.notna(latest.get('中单净流入-净额')) else 0,
                'medium_inflow_pct': float(latest.get('中单净流入-净占比', 0)) if pd.notna(latest.get('中单净流入-净占比')) else 0,
                'small_inflow': float(latest.get('小单净流入-净额', 0)) if pd.notna(latest.get('小单净流入-净额')) else 0,
                'small_inflow_pct': float(latest.get('小单净流入-净占比', 0)) if pd.notna(latest.get('小单净流入-净占比')) else 0,
            }
    except Exception as e:
        result['error'] = str(e)

    _set_cache(cache_key, result, ttl=60)
    return result


def get_market_sentiment() -> dict:
    """
    获取市场情绪指标
    通过沪深300、上证50、创业板指数判断大盘情绪
    返回: dict, 指数涨跌幅、情绪判断等
    """
    cache_key = "sentiment"
    cached = _get_cache(cache_key)
    if cached:
        return cached

    indices = {
        '000001': '上证指数',
        '399001': '深成指',
        '399006': '创业板指',
        '000300': '沪深300'
    }

    result = {
        'indices': [],
        'timestamp': datetime.now().isoformat()
    }

    # 腾讯接口 fallback session（复用连接）
    _tencent_session = requests.Session()
    _tencent_session.headers.update({
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://gu.qq.com/',
    })

    def _tencent_code(c: str) -> str:
        """构造腾讯股票/指数代码"""
        if c.startswith('0'):
            return f'sh{c}'
        elif c.startswith('3') or c.startswith('2'):
            return f'sz{c}'
        return f'sz{c}'

    def _fetch_from_tencent(code: str) -> dict | None:
        """从腾讯接口获取单个指数数据"""
        try:
            tc = _tencent_code(code)
            url = f'https://qt.gtimg.cn/q={tc}'
            resp = _tencent_session.get(url, timeout=5)
            resp.encoding = 'gb18030'
            text = resp.text
            if not text or 'v_' not in text:
                return None
            inner = text.split('="')[1].rstrip('"')
            parts = inner.split('~')
            if len(parts) < 38:
                return None
            price = float(parts[3]) if parts[3] else 0
            change_pct = float(parts[32]) if parts[32] else 0
            return {
                'price': price,
                'change_pct': change_pct
            }
        except Exception:
            return None

    try:
        for code, name in indices.items():
            index_data = None
            # 1. 先尝试东方财富
            try:
                secid = f"1.{code}" if code.startswith('0') else f"0.{code}"
                url = 'http://push2.eastmoney.com/api/qt/stock/get'
                params = {
                    'ut': 'bd1d9ddb04089700cf9c27f6f7426281',
                    'fltt': 2,
                    'invt': 2,
                    'secid': secid,
                    'fields': 'f43,f58,f170'
                }
                resp = requests.get(url, params=params, timeout=2)
                if resp.status_code == 200 and resp.text:
                    data = resp.json()
                    d = data.get('data', {})
                    if d:
                        index_data = {
                            'price': d.get('f43', 0),
                            'change_pct': d.get('f170', 0)
                        }
            except Exception as e:
                result.setdefault('partial_errors', []).append(f"{name}(东财): {e}")

            # 2. 东财失败 → 腾讯 fallback
            if index_data is None:
                index_data = _fetch_from_tencent(code)
                if index_data is None:
                    result.setdefault('partial_errors', []).append(f"{name}: 东财502+腾讯均失败")
                    continue

            result['indices'].append({
                'code': code,
                'name': name,
                'price': index_data['price'],
                'change_pct': index_data['change_pct']
            })

        # 判断情绪
        avg_change = sum(i['change_pct'] for i in result['indices']) / len(result['indices']) if result['indices'] else 0
        if avg_change > 0.5:
            result['sentiment'] = '强势'
        elif avg_change > 0:
            result['sentiment'] = '偏强'
        elif avg_change > -0.5:
            result['sentiment'] = '偏弱'
        else:
            result['sentiment'] = '弱势'

        result['avg_change'] = round(avg_change, 2)
    except Exception as e:
        result['error'] = str(e)

    _set_cache(cache_key, result, ttl=60)
    return result


if __name__ == '__main__':
    # 简单测试
    print("Testing akshare data module...")
    print("\n1. 获取个股行情:")
    prices = get_akshare_prices(['300059', '300033'])
    for code, data in prices.items():
        if 'error' in data:
            print(f"  {code}: ERROR {data['error']}")
        else:
            print(f"  {code}: {data.get('name')} ¥{data.get('price')} ({data.get('change_pct')}%)")

    print("\n2. 获取资金流向:")
    flow = get_capital_flow('300059')
    if 'error' in flow:
        print(f"  ERROR: {flow['error']}")
    else:
        summary = flow.get('summary', {})
        print(f"  主力净流入: {summary.get('main_inflow', 'N/A')}")
        print(f"  主力净流入占比: {summary.get('main_inflow_pct', 'N/A')}%")

    print("\n3. 市场情绪:")
    sentiment = get_market_sentiment()
    if 'error' in sentiment:
        print(f"  ERROR: {sentiment['error']}")
    else:
        for idx in sentiment.get('indices', []):
            print(f"  {idx['name']}: {idx['change_pct']}%")
        print(f"  整体情绪: {sentiment.get('sentiment')} (均值: {sentiment.get('avg_change')}%)")
