import test from "node:test";
import assert from "node:assert/strict";
import { documentFixture } from "./helpers/document-fixture.mjs";
import { verifiedFinancialDocument, attachDocumentBaselines, documentSnapshot, documentComparisonLabel, aggregatedRevenueDisagrees,
  financialFieldComparison } from "../src/document-evidence.mjs";

const cutoff = "2026-09-14T00:00:00Z";

test("literal official rows retain source units, PDF page and real comparative column", () => {
  const { stock, doc, source } = documentFixture();
  assert.equal(verifiedFinancialDocument(stock, [source], cutoff).doc, doc);
  assert.equal(aggregatedRevenueDisagrees(stock, doc), true);
  assert.match(documentComparisonLabel(stock, doc), /缺少前份封存/);
});

test("same amount still disagrees when supplier yoy is not the official column ratio", () => {
  const { stock, doc } = documentFixture();
  stock.financials.revenue = 2000;
  assert.equal(aggregatedRevenueDisagrees(stock, doc), false);
  stock.financials.revenueYoY = 1.24;
  stock.financials.units.revenueYoY = "percentage_point";
  assert.equal(aggregatedRevenueDisagrees(stock, doc), true);
  stock.financials.revenueYoY = 11.11;
  assert.equal(aggregatedRevenueDisagrees(stock, doc), false);
});

for (const [name, mutate] of [
  ["amount detached from quote", ({ doc }) => { doc.metrics[0].currentToken = "2,000.00"; }],
  ["page text tampering", ({ doc }) => { doc.pages[1].text += " altered"; }],
  ["wrong quote page", ({ doc }) => { doc.metrics[0].pageNumber = 1; }],
  ["source observation mismatch", ({ source }) => { source.observedAt = "2026-09-12T17:01:00+08:00"; }],
  ["media source laundering", ({ source }) => { source.sourceType = "media_aggregation"; }],
  ["invented publication instant", ({ doc }) => { doc.publishedAt = "2026-08-26T00:00:00+08:00"; }],
  ["unit changed without row evidence", ({ doc }) => { doc.metrics[0].unitLabel = "万元"; }],
  ["PDF source hash mismatch", ({ doc }) => { doc.pdfSha256 = "b".repeat(64); }],
  ["unsupported parser version", ({ doc }) => { doc.libraryVersion = "unknown"; }],
  ["wrong reporting period", ({ doc }) => { doc.comparisonReportDate = "2024-06-30"; }],
  ["query date detached from PDF", ({ doc }) => { doc.queryRecord.date = "2026-08-27"; }],
  ["query observed after PDF", ({ doc }) => { doc.queryRecord.observedAt = "2026-09-13T00:00:00Z"; }],
  ["non-official PDF origin", ({ doc }) => { doc.url = doc.url.replace("static.cninfo.com.cn", "example.com"); }],
]) {
  test(`rejects ${name}`, () => {
    const fixture = documentFixture(); mutate(fixture);
    assert.throws(() => verifiedFinancialDocument(fixture.stock, [fixture.source], cutoff), /原文证据/);
  });
}

test("a same-day date-only PDF first collected after cutoff is not a cutoff fact", () => {
  const { stock, doc, source, context } = documentFixture();
  doc.queryRecord.observedAt = "2026-08-26T16:59:59+08:00";
  doc.observedAt = doc.dataAsOf = source.observedAt = source.dataAsOf = "2026-08-26T17:00:00+08:00";
  assert.equal(verifiedFinancialDocument(stock, [source], "2026-08-26T00:00:00Z"), null);
  assert.equal(documentSnapshot(context, stock.code, "2026-08-26T00:00:00Z"), null);
});

test("raw input comparison tags are cleared; same/future/invalid baseline dates cannot suppress rows", () => {
  const { context, stock, doc } = documentFixture();
  stock.previousOfficialDocument = { url: doc.url, pdfSha256: doc.pdfSha256, targetDate: "2026-09-11" };
  const before = JSON.stringify(context);
  for (const date of ["2026-09-14", "2026-09-15", "bad"]) {
    const result = attachDocumentBaselines(context, { [stock.code]: { baselineTargetDate: date, baselineReportSha256: "b".repeat(64), officialDocument: documentSnapshot(context, stock.code, cutoff) } });
    assert.equal(result.data.watchlist[0].previousOfficialDocument, null);
  }
  assert.equal(JSON.stringify(context), before);
});

test("comparison identifies the actual prior document and detects same-URL byte changes", () => {
  const { context, stock, doc } = documentFixture();
  const result = attachDocumentBaselines(context, { [stock.code]: {
    baselineTargetDate: "2026-09-11", baselineReportSha256: "b".repeat(64), officialDocument: documentSnapshot(context, stock.code, cutoff),
  } });
  const current = result.data.watchlist[0];
  assert.match(documentComparisonLabel(current, doc), /2026-09-11.*一致/);
  assert.match(documentComparisonLabel(current, { ...doc, pdfSha256: "c".repeat(64) }), /内容不同.*核验修订/);
  assert.match(documentComparisonLabel(current, { ...doc, url: doc.url.replace("0.PDF", "1.PDF") }), /新增收录.*不等于新公告/);
});

test("逐字段核验：先核对维度再核验金额，金额接近不算口径一致", () => {
  const { stock, doc } = documentFixture();
  // 维度无冲突、仅金额相差：是“数值不一致，差异原因待核”，不是“口径不同”
  const mismatch = financialFieldComparison(stock, doc);
  assert.equal(mismatch.label, "数值不一致，差异原因待核");
  assert.ok(!/口径不同/.test(mismatch.label), "维度没有冲突时不得判成口径不同");
  assert.match(mismatch.dimensionSummary, /报告期一致/);
  assert.match(mismatch.dimensionSummary, /币种一致/);
  assert.match(mismatch.dimensionSummary, /合并范围尚未核验/);
  assert.match(mismatch.dimensionSummary, /单位一致/);
  assert.match(mismatch.dimensionSummary, /是否重述尚未核验/);
  assert.equal(mismatch.amounts.official, 2000);
  assert.equal(mismatch.amounts.provider, 2100);
  assert.equal(mismatch.amounts.amountsEqual, false);
  assert.equal(mismatch.amounts.delta, 100);
  assert.deepEqual(mismatch.trace, {
    pageNumber: 2, quote: "营业收入 2,000.00 1,800.00", currentToken: "2,000.00",
    previousToken: "1,800.00", pageTextSha256: doc.metrics[1].pageTextSha256,
    pdfSha256: doc.pdfSha256, url: doc.url, sourceId: "synthetic-pdf"
  });
  assert.ok(!/口径一致/.test(mismatch.label), "不得写成口径一致");

  // 金额与同比都对上：才叫数值一致（未核验维度仍然要列出来）
  stock.financials.revenue = 2000;
  stock.financials.revenueYoY = 11.11;
  stock.financials.units.revenueYoY = "percentage_point";
  const aligned = financialFieldComparison(stock, doc);
  assert.equal(aligned.label, "数值一致");
  assert.ok(aligned.unverifiedDimensions.length > 0, "未核验维度仍然要列出来");

  // 金额一致但同比缺一侧：不得笼统写“数值一致”
  delete stock.financials.revenueYoY;
  const noYoy = financialFieldComparison(stock, doc);
  assert.equal(noYoy.label, "本期金额一致；同比尚未核验");
  assert.equal(noYoy.amounts.yoyComparable, false);
  assert.ok(noYoy.notes.some((note) => /同比尚未核验|未提供同比/.test(note)),
    "必须提示同比缺失");

  // 金额一致但同比不同：仍算数值不一致
  stock.financials.revenueYoY = 3.5;
  stock.financials.units.revenueYoY = "percentage_point";
  const yoyDiffers = financialFieldComparison(stock, doc);
  assert.equal(yoyDiffers.label, "数值不一致，差异原因待核");
  assert.ok(yoyDiffers.notes.some((note) => /同比不同/.test(note)));

  // 维度冲突（报告期不同）才是口径不同
  stock.financials.revenueYoY = 11.11;
  stock.financials.reportDate = "2026-03-31";
  assert.equal(financialFieldComparison(stock, doc).label, "口径不同");

  // 未声明单位时不得按数量级比较金额
  stock.financials.revenue = 2100;
  stock.financials.revenueYoY = 1.24;
  stock.financials.reportDate = "2026-06-30";
  delete stock.financials.units.revenue;
  const noUnit = financialFieldComparison(stock, doc);
  assert.equal(noUnit.label, "供应商单位待核，暂不直接比较");
  assert.equal(noUnit.dimensions.unit.state, "unverified");
  stock.financials.units.revenue = "CNY";

  // 缺供应商金额：尚未核验
  stock.financials.reportDate = "2026-06-30";
  delete stock.financials.revenue;
  assert.equal(financialFieldComparison(stock, doc).label, "尚未核验");
});
