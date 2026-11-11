import { sha256 } from "../../src/snapshot-integrity.mjs";

// Synthetic amounts AND synthetic PDF bytes; never historical/production proof.
export function documentFixture() {
  const stock = { code: "002617", name: "露笑科技", financials: { reportDate: "2026-06-30", revenue: 2100, units: { revenue: "CNY" } } };
  const quote = "经营活动产生的现金流量净额 1,000.00 900.00";
  const revenueQuote = "营业收入 2,000.00 1,800.00";
  const body = Buffer.from("%PDF-1.7\nsynthetic evidence test bytes\n");
  const pages = [
    "证券代码：002617 露笑科技2026年半年度报告摘要 本报告期 上年同期 单位：元 币种：人民币",
    `${quote} ${revenueQuote}`,
  ].map((text, index) => ({ pageNumber: index + 1, text, textSha256: sha256(text) }));
  const doc = {
    schemaVersion: "1.0", status: "available", kind: "financial_summary", code: stock.code, name: stock.name,
    title: "2026年半年度报告摘要", url: "https://static.cninfo.com.cn/finalpage/2026-08-26/0.PDF",
    disclosureDate: "2026-08-26", publishedAt: null, timePrecision: "date",
    observedAt: "2026-09-12T17:00:00+08:00", dataAsOf: "2026-09-12T17:00:00+08:00",
    pdfSha256: sha256(body), pdfBytes: body.length, parserVersion: "cninfo-pdf-1", libraryVersion: "6.18.1",
    reportDate: "2026-06-30", comparisonReportDate: "2025-06-30",
    basis: "本报告期/上年同期（同份半年报摘要原列）", pages, sourceIds: ["synthetic-pdf"],
    metrics: [
      { field: "operatingCashFlow", label: "经营活动产生的现金流量净额", currentToken: "1,000.00", previousToken: "900.00", quote },
      { field: "revenue", label: "营业收入", currentToken: "2,000.00", previousToken: "1,800.00", quote: revenueQuote },
    ].map((m) => ({ ...m, unitLabel: "元", currency: "CNY", pageNumber: 2, pageTextSha256: pages[1].textSha256 })),
  };
  doc.queryRecord = { title: doc.title, date: doc.disclosureDate, url: doc.url, observedAt: "2026-09-12T16:59:59+08:00", dateKind: "source_disclosure_date" };
  const source = {
    id: "synthetic-pdf", title: doc.title, url: doc.url, publisher: "巨潮资讯网", sourceType: "official_announcement",
    publishedAt: null, timePrecision: "date", disclosureDate: doc.disclosureDate,
    observedAt: doc.observedAt, dataAsOf: doc.observedAt, pdfSha256: doc.pdfSha256,
  };
  stock.officialDisclosure = { financialSummary: doc };
  return { stock, doc, source, body, context: { meta: { targetDate: "2026-09-14" }, data: { watchlist: [stock] }, sources: [source] } };
}
