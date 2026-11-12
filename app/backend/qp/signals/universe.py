"""Frozen swing target universe (M4 shadow).

Copied from the legacy ``config.swing.json`` ``targets`` list on 2026-11-12 so the shadow
run evaluates the same 40 ETFs and sectors.  Keep this list immutable during the shadow
window; changing it invalidates the day-over-day comparison.
"""
from __future__ import annotations

from dataclasses import dataclass

MA_LONG = 60
VOLUME_WINDOW = 20
VOLUME_RATIO = 1.5
DAILY_BREAKOUT_ENABLED = False  # legacy config.technical.daily_volume_breakout
WEEKLY_MACD_ENABLED = True


@dataclass(frozen=True)
class Target:
    code: str
    name: str
    sector: str
    mode: str = "trend"


TARGETS: tuple[Target, ...] = (
    Target("510300", "沪深300ETF", "宽基"),
    Target("510500", "中证500ETF", "宽基"),
    Target("159915", "创业板ETF", "成长"),
    Target("588000", "科创50ETF", "硬科技"),
    Target("512480", "半导体ETF", "行业"),
    Target("512890", "红利低波ETF", "防御"),
    Target("513180", "恒生科技ETF", "港股科技"),
    Target("512000", "券商ETF", "情绪"),
    Target("512170", "医疗ETF", "行业"),
    Target("518880", "黄金ETF", "避险"),
    Target("515880", "通信ETF", "硬科技"),
    Target("512100", "中证1000ETF", "宽基"),
    Target("510050", "上证50ETF", "宽基"),
    Target("159949", "创业板50ETF", "成长"),
    Target("513050", "中概互联网ETF", "港股科技"),
    Target("512050", "A500ETF", "宽基"),
    Target("159941", "纳指ETF", "跨境"),
    Target("512800", "银行ETF", "防御"),
    Target("515050", "5G通信ETF", "硬科技"),
    Target("515220", "煤炭ETF", "周期"),
    Target("512400", "有色金属ETF", "周期"),
    Target("159985", "豆粕ETF", "商品"),
    Target("510880", "红利ETF", "防御"),
    Target("159819", "人工智能ETF", "硬科技"),
    Target("562500", "机器人ETF", "硬科技"),
    Target("512010", "医药ETF", "行业"),
    Target("159869", "游戏ETF", "成长"),
    Target("512690", "酒ETF", "消费"),
    Target("515080", "央企红利ETF", "防御"),
    Target("159611", "电力ETF", "防御"),
    Target("159755", "电池ETF", "成长"),
    Target("159901", "深证100ETF", "宽基"),
    Target("513500", "标普500ETF", "跨境"),
    Target("512980", "传媒ETF", "成长"),
    Target("512200", "房地产ETF", "周期"),
    Target("159870", "化工ETF", "周期"),
    Target("512660", "军工ETF", "行业"),
    Target("513520", "日经ETF", "跨境"),
    Target("159928", "消费ETF", "消费"),
    Target("159920", "恒生ETF", "港股"),
)
