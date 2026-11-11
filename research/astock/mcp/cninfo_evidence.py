"""Bounded, literal CNINFO PDF evidence; no model summaries or inferred numbers."""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

import requests

from cninfo_source import build_cninfo_session

VERSION = "cninfo-pdf-1"
MAX_BYTES = 4 * 1024 * 1024
MAX_PAGES = 12
MAX_TEXT = 120_000
URL = re.compile(r"https://static\.cninfo\.com\.cn/finalpage/(\d{4}-\d{2}-\d{2})/(\d+)\.PDF\Z", re.I)
NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}"
METRICS = {
    "operatingCashFlow": "经营活动产生的现金流量净额",
    "revenue": "营业收入",
    "parentNetProfit": "归属于上市公司股东的净利润",
}


def digest(body):
    return hashlib.sha256(body).hexdigest()


def normalize_text(text):
    # PDF writers may space or wrap Chinese glyphs within a single table cell.
    # Preserve numeric cell separators; do NOT concatenate adjacent amounts.
    text = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_financial_summary(pages, code, name, title, disclosure_date):
    if not re.fullmatch(r"20\d{2}年半年度报告摘要", re.sub(r"\s+", "", title)):
        raise ValueError("unsupported_document_kind")
    dt.date.fromisoformat(disclosure_date)
    year = int(re.search(r"20\d{2}", title).group())
    report_date = f"{year}-06-30"
    if report_date > disclosure_date:
        raise ValueError("report_period_after_disclosure")
    first = pages[0]["text"][:1200]
    if not re.search(rf"(?:证券|公司|股票)代码\s*[：:]?\s*{re.escape(code)}(?:\D|$)", first) or name not in first:
        raise ValueError("document_security_mismatch")
    if re.sub(r"\s+", "", title) not in re.sub(r"\s+", "", first):
        raise ValueError("document_title_mismatch")
    combined = " ".join(p["text"] for p in pages)
    if not re.search(r"本报告期\s*上年同期", combined):
        raise ValueError("comparative_columns_unverified")
    if re.search(r"调整前|调整后|重述前|重述后", combined):
        raise ValueError("unsupported_restatement_columns")
    currencies = re.findall(r"币种\s*[:：]\s*([^\s，,；;）)]+)", combined)
    if any(not value.startswith("人民币") for value in currencies):
        raise ValueError("unsupported_declared_currency")
    header_yuan = bool(re.search(r"单位\s*[:：]\s*元\s*币种\s*[:：]\s*人民币", combined))
    metrics = []
    for key, label in METRICS.items():
        matches = []
        pattern = re.compile(re.escape(label) + rf"\s*(?P<unit>[（(]元[）)])?\s+(?P<current>{NUMBER})\s+(?P<previous>{NUMBER})(?![\d.,])")
        for page in pages:
            for match in pattern.finditer(page["text"]):
                if not (match.group("unit") or header_yuan):
                    continue
                matches.append({
                    "field": key, "label": label, "currentToken": match.group("current"),
                    "previousToken": match.group("previous"), "unitLabel": "元",
                    "currency": "CNY" if currencies else "not_separately_stated",
                    "pageNumber": page["pageNumber"], "quote": match.group(),
                    "pageTextSha256": page["textSha256"],
                })
        if len(matches) == 1:
            metrics.append(matches[0])
    if not any(m["field"] == "operatingCashFlow" for m in metrics):
        raise ValueError("cash_flow_row_unverified")
    return {
        "reportDate": report_date, "comparisonReportDate": f"{year - 1}-06-30",
        "basis": "本报告期/上年同期（同份半年报摘要原列）", "metrics": metrics,
    }


def _extract(path):
    # Limit malformed PDF resource consumption independently of network bounds.
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    except ImportError:
        pass
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(Path(path).read_bytes()), strict=True)
    if reader.is_encrypted or not 1 <= len(reader.pages) <= MAX_PAGES:
        raise ValueError("pdf_page_contract")
    pages = []
    total = 0
    for index, page in enumerate(reader.pages):
        text = normalize_text(page.extract_text() or "")
        total += len(text)
        if total > MAX_TEXT:
            raise ValueError("pdf_text_limit")
        pages.append({"pageNumber": index + 1, "text": text, "textSha256": digest(text.encode())})
    return {"parserVersion": VERSION, "libraryVersion": pypdf.__version__, "pages": pages}


def _private_write_once(path, body):
    fd, temporary = tempfile.mkstemp(prefix=".pdf-incoming-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != body:
                raise ValueError("evidence_cache_collision")
    finally:
        Path(temporary).unlink(missing_ok=True)


def _collect_summary(row, stock, cache_dir):
    """Only a small official summary, with actual observation and retained bytes."""
    url = row.get("url", "")
    match = URL.fullmatch(url)
    if not match or match[1] != row.get("date"):
        raise ValueError("untrusted_pdf_url")
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise ValueError("evidence_cache_symlink")
    started = time.monotonic()
    with build_cninfo_session() as session:
        session.trust_env = False
        response = session.get(url, timeout=(3, 4), stream=True, allow_redirects=False)
        closer = getattr(response, "close", None)
        try:
            if response.status_code != 200:
                raise ValueError("pdf_http_unavailable")
            if int(response.headers.get("Content-Length", "0")) > MAX_BYTES:
                raise ValueError("pdf_byte_limit")
            body = bytearray()
            for chunk in response.iter_content(65536):
                if time.monotonic() - started > 8:
                    raise ValueError("pdf_download_deadline")
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise ValueError("pdf_byte_limit")
        finally:
            if callable(closer):
                closer()
    body = bytes(body)
    if not body.startswith(b"%PDF-"):
        raise ValueError("response_not_pdf")
    fetched = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat()
    pdf_hash = digest(body)
    pdf_path = directory / f"{pdf_hash}.pdf"
    _private_write_once(pdf_path, body)
    worker = subprocess.run([sys.executable, "-B", __file__, "--extract", str(pdf_path)],
                            capture_output=True, text=True, timeout=7, check=False)
    if worker.returncode or len(worker.stdout) > MAX_TEXT * 8:
        raise ValueError("pdf_extraction_failed")
    extracted = json.loads(worker.stdout)
    financial = parse_financial_summary(extracted["pages"], stock["code"], stock["name"], row["title"], row["date"])
    return {
        "schemaVersion": "1.0", "status": "available", "kind": "financial_summary",
        "code": stock["code"], "name": stock["name"], "title": row["title"],
        "url": url, "disclosureDate": row["date"], "publishedAt": None, "timePrecision": "date",
        "observedAt": fetched, "dataAsOf": fetched, "pdfSha256": pdf_hash,
        "queryRecord": {key: row.get(key) for key in ("title", "date", "url", "observedAt", "dateKind")},
        "pdfBytes": len(body), "collectorSourceSha256": digest(Path(__file__).read_bytes()),
        "pythonVersion": sys.version.split()[0], **extracted, **financial,
    }


def collect_summary(row, stock, cache_dir, *, timeout_seconds=10):
    """Wall-clock boundary includes slow-drip HTTP and the extraction child."""
    match = URL.fullmatch(row.get("url", ""))
    if not match or match[1] != row.get("date"):
        raise ValueError("untrusted_pdf_url")
    if os.name != "posix" or not 0 < timeout_seconds <= 10:
        raise ValueError("pdf_process_boundary_unavailable")
    payload = json.dumps({"row": row, "stock": {"code": stock["code"], "name": stock["name"]}, "cacheDir": str(cache_dir)}, ensure_ascii=False)
    if len(payload) > 16384:
        raise ValueError("pdf_request_limit")
    with subprocess.Popen([sys.executable, "-B", __file__, "--collect"], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True) as process:
        try:
            stdout, _stderr = process.communicate(payload, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            # Kill the whole group, including an extraction grandchild.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise ValueError("pdf_collection_timeout") from None
        if len(stdout) > MAX_TEXT * 8:
            raise ValueError("pdf_result_limit")
        result = json.loads(stdout)
        if process.returncode:
            reason = result.get("reason", "")
            raise ValueError(reason if re.fullmatch(r"[a-z_]{1,64}", reason) else "pdf_collection_or_extraction_failed")
        return result


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--extract":
            result = _extract(sys.argv[2])
        elif sys.argv[1:] == ["--collect"]:
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
            raw = sys.stdin.read(16385)
            if len(raw) > 16384:
                raise ValueError("pdf_request_limit")
            request = json.loads(raw)
            result = _collect_summary(request["row"], request["stock"], request["cacheDir"])
        else:
            raise ValueError("invalid_pdf_worker_command")
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        reason = str(error) if isinstance(error, ValueError) and re.fullmatch(r"[a-z_]{1,64}", str(error)) else "pdf_collection_or_extraction_failed"
        print(json.dumps({"status": "unavailable", "reason": reason}))
        raise SystemExit(1)
