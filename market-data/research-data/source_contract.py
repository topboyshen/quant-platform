"""Validation for new THS observations, not retroactive historical rewriting."""
import datetime
import re

from ths_source import security_market


def validate_current_source_payloads(concepts, insiders, codes):
    for source, records, kind in (("concepts", concepts, "concept"),
                                   ("insiders", insiders, "holder")):
        if not isinstance(records, dict) or set(records) != set(codes):
            raise ValueError(f"{source} security coverage mismatch")
        for code in codes:
            value = records[code]
            if not isinstance(value, dict):
                raise ValueError(f"{source} invalid payload")
            evidence = value.get("evidence")
            market, _ = security_market(code)
            if (not isinstance(evidence, dict) or evidence.get("provider") != "ths_f10"
                    or evidence.get("status") != "validated" or evidence.get("code") != code
                    or evidence.get("market") != market or evidence.get("parserVersion") != 1
                    or evidence.get("timeKind") != "observed_at"
                    or evidence.get("sourceUrl") != f"https://basic.10jqka.com.cn/{code}/{kind}.html"
                    or not isinstance(evidence.get("responseSha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", evidence["responseSha256"])):
                raise ValueError(f"{source} invalid provenance")
            try:
                observed = datetime.datetime.fromisoformat(evidence["observedAt"])
                started = datetime.datetime.fromisoformat(evidence["collectionStartedAt"])
                if observed.utcoffset() is None or started.utcoffset() is None or started > observed:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise ValueError(f"{source} invalid observation window") from None
            if source == "concepts":
                for category in ("概念", "其他概念"):
                    names = value.get(category)
                    if (not isinstance(names, list) or not 1 <= len(names) <= 200
                            or any(not isinstance(n, str) or not n.strip() or len(n) > 128 for n in names)
                            or len(set(names)) != len(names)):
                        raise ValueError("concepts invalid membership")
                if evidence.get("taxonomy") != "ths_f10_current_concepts" or evidence.get("truncated") is not False:
                    raise ValueError("concepts invalid taxonomy")
            else:
                text = value.get("text")
                length = evidence.get("fullTextChars")
                if (not isinstance(text, str) or not text.strip() or len(text) > 3000
                        or type(length) is not int or length < len(text)
                        or type(evidence.get("truncated")) is not bool
                        or evidence["truncated"] != (length > 3000)
                        or len(text) != min(length, 3000)
                        or evidence.get("sourceSection") != "股东研究"
                        or not evidence.get("reportDates") or not evidence.get("tableSummaries")):
                    raise ValueError("insiders invalid shareholder excerpt")
