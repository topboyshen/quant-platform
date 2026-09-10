"""THS current concept membership; taxonomy explicitly differs from Baidu."""
import re

from ths_source import F10Client, SourceError


class ThsConceptClient(F10Client):
    def __init__(self):
        super().__init__("concepts")

    def concepts(self, code):
        soup, evidence = self.page(code, "concept")
        return self.parse(soup, evidence)

    @staticmethod
    def parse(soup, evidence):
        def reject():
            raise SourceError("concepts", "concept_page", "InvalidSchema", evidence.get("code"))
        result, identifiers = {}, {}
        for ident, heading, output, cls, idattr in (
            ("concept", "常规概念", "概念", "gnName", "clid"),
            ("other", "其他概念", "其他概念", "gnStockList", "cid"),
        ):
            sections = soup.find_all(id=ident)
            if len(sections) != 1:
                reject()
            section = sections[0]
            headings = section.find_all("h2")
            if len(headings) != 1 or headings[0].get_text(strip=True) != heading:
                reject()
            tables = section.select("table.gnContent")
            if len(tables) != 1:
                reject()
            table = tables[0]
            headers = [th.get_text(strip=True) for th in table.select("thead th")]
            expected_headers = (["序号", "概念名称", "龙头股", "概念解析"] if ident == "concept"
                                else ["序号", "概念名称", "概念解析"])
            if headers != expected_headers:
                reject()
            bodies = table.find_all("tbody", recursive=False)
            if len(bodies) != 1:
                reject()
            names, ids = [], []
            for row in bodies[0].find_all("tr", recursive=False):
                if "extend_content" in row.get("class", []):
                    detail_cells = row.find_all("td", recursive=False)
                    if (len(detail_cells) != 1 or detail_cells[0].get("colspan") != str(len(headers))
                            or row.select("td.gnName, td.gnStockList, [clid], [cid]")):
                        reject()
                    continue
                cells = row.find_all("td", recursive=False)
                named = row.select("td." + cls)
                if (len(cells) != len(headers) or len(named) != 1
                        or cells[1] is not named[0]
                        or cells[0].get_text(strip=True) != str(len(names) + 1)):
                    reject()
                name = named[0].get_text(" ", strip=True)
                source_id = named[0].get(idattr, "")
                if (not 1 <= len(name) <= 128 or any(ord(c) < 32 for c in name)
                        or not re.fullmatch(r"[0-9]{6}", source_id)
                        or name in names or source_id in ids or len(names) >= 200):
                    reject()
                names.append(name)
                ids.append(source_id)
            # No verified empty-result contract has been observed. Fail closed;
            # an empty table or JS shell is not evidence of no membership.
            if not names:
                reject()
            result[output] = names
            identifiers[output] = ids
        result["evidence"] = {**evidence, "taxonomy": "ths_f10_current_concepts",
                              "categoryMapping": {"概念": "常规概念", "其他概念": "其他概念"},
                              "categoryIds": identifiers, "truncated": False,
                              "scope": "regular_and_other_concepts_only",
                              "historicalMembershipVerified": False}
        return result
