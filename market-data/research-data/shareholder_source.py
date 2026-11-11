"""Bounded excerpts of two THS shareholder tables, not a full ownership history."""
import datetime as dt
from decimal import Decimal, InvalidOperation
import re

from bs4 import Comment

from ths_source import F10Client, SourceError, security_market


MAX_TEXT_CHARS = 3000
CN = dt.timezone(dt.timedelta(hours=8))
_NUMBER = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?"
_IGNORED_TAGS = {"script", "style", "noscript", "template"}


def _reject(code=None, error_type="InvalidSchema"):
    raise SourceError("insiders", "holder_page", error_type, code=code, http_status=200)


def _text(node, code, maximum=500):
    parts = []
    for value in node.find_all(string=True):
        if isinstance(value, Comment):
            continue
        parent = value.parent
        ignored = False
        while parent is not None:
            if parent.name in _IGNORED_TAGS:
                ignored = True
                break
            if parent is node:
                break
            parent = parent.parent
        if not ignored:
            parts.append(str(value))
    value = " ".join(" ".join(parts).split())
    if (not value or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        _reject(code)
    return value


def _date(value, observed_day, code):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        _reject(code)
    try:
        day = dt.date.fromisoformat(value)
    except ValueError:
        _reject(code)
    if day > observed_day:
        _reject(code, "FutureReportDate")
    return day


def _number(value, code, *, percent=False):
    pattern = _NUMBER + (r"%" if percent else r"(?:万|亿)?(?:股)?")
    if len(value) > 60 or not re.fullmatch(pattern, value):
        _reject(code)
    number = re.match(_NUMBER, value)[0].replace(",", "")
    try:
        parsed = Decimal(number)
    except InvalidOperation:
        _reject(code)
    if not parsed.is_finite() or (not 0 <= parsed <= 100 if percent else parsed <= 0):
        _reject(code)
    return value


def _identity(evidence):
    code = evidence.get("code") if isinstance(evidence, dict) else None
    try:
        market, _ = security_market(code)
    except ValueError:
        _reject()
    if (evidence.get("provider") != "ths_f10" or evidence.get("market") != market
            or evidence.get("sourceUrl") != f"https://basic.10jqka.com.cn/{code}/holder.html"
            or type(evidence.get("parserVersion")) is not int or evidence["parserVersion"] != 1
            or evidence.get("status") != "validated" or evidence.get("timeKind") != "observed_at"
            or not isinstance(evidence.get("responseSha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", evidence["responseSha256"]) is None
            or not isinstance(evidence.get("title"), str)
            or re.fullmatch(r"[^<>]{1,80}\(" + code + r"\) 股东研究_F10_同花顺金融服务网", evidence["title"]) is None):
        _reject(code)
    try:
        observed = dt.datetime.fromisoformat(evidence["observedAt"])
        if observed.tzinfo is None or observed.utcoffset() is None:
            _reject(code)
        day = observed.astimezone(CN).date()
    except (KeyError, TypeError, ValueError, OverflowError):
        _reject(code)
    return code, day


def _section(soup, section_id, heading, prefix, date_class, ratio_header, observed_day, code):
    sections = soup.find_all(id=section_id)
    if len(sections) != 1:
        _reject(code)
    section = sections[0]
    headings = section.find_all("h2")
    if len(headings) != 1 or _text(headings[0], code, 100) != heading:
        _reject(code)
    tabs = section.select("div.m_tab li[name]")
    if not 1 <= len(tabs) <= 5:
        _reject(code)
    dates, current = [], []
    for index, tab in enumerate(tabs, 1):
        target = f"{prefix}_{index}"
        links = tab.find_all("a", class_=date_class)
        if tab.get("name") != target or len(links) != 1 or links[0].get("targ") != target:
            _reject(code)
        dates.append(_date(_text(links[0], code, 10), observed_day, code))
        if "cur" in tab.get("class", []):
            current.append(index)
    if dates != sorted(set(dates), reverse=True) or current != [1]:
        _reject(code)
    # The source binds the date tab to this exact panel; do not search the page
    # for an unrelated date or treat a caption's narrative numbers as a table.
    panels = soup.find_all(id=f"{prefix}_1")
    if len(panels) != 1 or section not in panels[0].parents:
        _reject(code)
    tables = panels[0].select("table.m_table.m_hl.ggintro")
    if len(tables) != 1:
        _reject(code)
    table = tables[0]
    captions = table.find_all("caption", recursive=False)
    heads = table.find_all("thead", recursive=False)
    bodies = table.find_all("tbody", recursive=False)
    if len(captions) != 1 or len(heads) != 1 or len(bodies) != 1:
        _reject(code)
    caption = _text(captions[0], code, 300)
    if heading == "十大流通股东":
        if not caption.startswith("前十大流通股东累计持有"):
            _reject(code)
        kind = "holdings_snapshot"
    elif caption.startswith("前十大股东累计持有"):
        kind = "holdings_snapshot"
    elif caption == "主要股东持股变动公告":
        kind = "holdings_change_announcement"
    else:
        _reject(code)
    header_rows = heads[0].find_all("tr", recursive=False)
    if len(header_rows) != 1:
        _reject(code)
    cells = header_rows[0].find_all(["th", "td"], recursive=False)
    expected = ["机构或基金名称", "持有数量(股)", "持股变化(股)", ratio_header]
    if len(cells) < 4 or [_text(cell, code, 80) for cell in cells[:4]] != expected:
        _reject(code)
    rows = bodies[0].find_all("tr", recursive=False)
    if not 1 <= len(rows) <= 10:
        _reject(code)
    records, names = [], set()
    for row in rows:
        cells = row.find_all(["th", "td"], recursive=False)
        # THS sometimes leaves a later td unclosed. Only the first four direct
        # cells have a verified binding; later nested detail cells are omitted.
        if len(cells) < 4 or any(cell.find(["td", "th", "table"]) for cell in cells[:4]):
            _reject(code)
        if any(cell.get("rowspan", "1") != "1" or cell.get("colspan", "1") != "1" for cell in cells[:4]):
            _reject(code)
        name = _text(cells[0], code)
        quantity = _number(_text(cells[1], code, 60), code)
        ratio = _number(_text(cells[3], code, 60), code, percent=True)
        if name in names or name in {"合计", "总计", "暂无数据", "暂无相关数据"}:
            _reject(code)
        names.add(name)
        records.append(f"{name} | {quantity} | {ratio}")
    date_label = dates[0].isoformat()
    text = "\n".join([f"{heading}｜源表日期：{date_label}", f"表题：{caption}",
                       f"机构或基金名称 | 持有数量(股) | {ratio_header}", *records])
    summary = {"section": heading, "reportDate": date_label, "caption": caption,
               "tableKind": kind, "rowCount": len(records),
               "columnsIncluded": [expected[0], expected[1], expected[3]]}
    return text, summary


class ShareholderClient(F10Client):
    def __init__(self):
        super().__init__("insiders")

    def shareholders(self, code):
        soup, evidence = self.page(code, "holder")
        return self.parse(soup, evidence)

    @staticmethod
    def parse(soup, evidence):
        """Parse only verified panels. The supplied common evidence is immutable."""
        try:
            code, observed_day = _identity(evidence)
            selected = [
                _section(soup, "flowholder", "十大流通股东", "fher", "fdates", "占流通股比例", observed_day, code),
                _section(soup, "tenholder", "十大股东", "ther", "tdates", "占总股本比例", observed_day, code),
            ]
            full_text = "\n\n".join(item[0] for item in selected)
            summaries = [item[1] for item in selected]
            return {"text": full_text[:MAX_TEXT_CHARS], "evidence": {
                **evidence, "sourceSection": "股东研究",
                "truncated": len(full_text) > MAX_TEXT_CHARS, "fullTextChars": len(full_text),
                "reportDates": sorted({item["reportDate"] for item in summaries}, reverse=True),
                "reportDateMeaning": "source_table_date_not_first_observed_time",
                "scope": "latest_visible_shareholder_tables_excerpt",
                "completeHistory": False, "tableSummaries": summaries,
            }}
        except SourceError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            code = evidence.get("code") if isinstance(evidence, dict) else None
            _reject(code)
