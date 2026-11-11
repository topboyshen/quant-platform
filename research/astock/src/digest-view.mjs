/**
 * Sealed research digest view (producer side).
 *
 * The digest is a structured, machine-readable view of the day's sealed
 * research: conclusion lines, per-item inference with the compiler's own
 * [来源 N] references, counter-evidence, and explicit limitations. It is
 * rendered by the same deterministic compiler pass that produces the sealed
 * report (see compileFrozenReport capture), so the source numbering cannot
 * drift from the report.
 *
 * Contract rules:
 * - Only frozen artifacts are read: the digest is built from the archived
 *   input plus the sealed selection, never by re-reading or re-parsing the
 *   report body and never by recompiling an old archive with new rules.
 * - Missing real counter-evidence is carried as an explicit limitation; it is
 *   never fabricated.
 * - Unknown source ids, post-cutoff sources or hash conflicts abort the build.
 * - The digest body does not contain its own hash; the seal manifest records
 *   digestSha256, which avoids a self-referential hash.
 */
import { canonicalJson, jsonHash, sha256 } from "./snapshot-integrity.mjs";
import { compileFrozenReport } from "./run-archive.mjs";
import { reportViewFromFrozenConfiguration } from "./frozen-notification-presentation.mjs";

export const DIGEST_SCHEMA_VERSION = "astock.research-digest.v1";
export const DIGEST_KIND = "research_digest";

const LIMITATION_HEADLINE = "本摘要为结构化封存视图；未提供结构化反证来源的条目只保留推断限制，不升级为事实结论。";

function assertSha256(value, label) {
  if (typeof value !== "string" || !/^[0-9a-f]{64}$/.test(value)) {
    throw new Error(`${label} 必须是 64 位小写十六进制 sha256`);
  }
  return value;
}

function isoDate(value, label) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    throw new Error(`${label} 必须是 YYYY-MM-DD`);
  }
  return value;
}

export function buildDigestView({ input, selection, inputSha256, selectionSha256, reportSha256, generatedAt = null }) {
  if (!input || typeof input !== "object") throw new Error("digest 缺少冻结输入");
  assertSha256(inputSha256, "inputSha256");
  assertSha256(selectionSha256, "selectionSha256");
  assertSha256(reportSha256, "reportSha256");
  if (jsonHash(selection ?? null) !== selectionSha256) throw new Error("digest selection 与封存选择不一致");

  const context = input.compilerContext;
  const reportDate = isoDate(input.targetDate, "targetDate");
  const dataDate = context?.data?.previousAShareSession?.sessionDate ?? null;
  if (dataDate !== null) isoDate(dataDate, "dataDate");

  const capture = {};
  const compiled = compileFrozenReport(input, selection ?? null, capture);

  const items = (capture.overlay ?? []).map((item) => ({
    kind: item.kind,
    label: item.label ?? null,
    code: item.code ?? null,
    inference: item.inference,
    evidence: (item.evidence ?? []).map((entry) => ({
      sourceId: entry.id,
      reference: entry.marker,
      role: entry.role ?? null
    })),
    counterEvidence: (item.counterEvidence ?? []).map((entry) => ({
      sourceId: entry.id,
      reference: entry.marker
    })),
    periodNote: item.periodNote ?? null,
    limitation: item.limitation ?? null
  }));

  const limitations = [];  const withoutCounter = items.filter((item) => item.counterEvidence.length === 0);
  if (withoutCounter.length > 0) {
    for (const text of new Set(withoutCounter.map((item) => item.limitation).filter(Boolean))) limitations.push(text);
    limitations.push(LIMITATION_HEADLINE);
  }
  if (!capture.overlayHasContent) {
    limitations.push("本次为程序核验基础版，第 6 节没有结构化推断条目。");
  }

  // Evidence index for machine consumers: number -> the frozen source identity it
  // was assigned in this same compilation pass. Consumers never re-number.
  const sourceIndex = (capture.sources ?? []).map((entry) => ({ ...entry }));
  const numbered = new Map(sourceIndex.map((entry) => [entry.number, entry]));
  for (const item of items) {
    for (const entry of [...item.evidence, ...item.counterEvidence]) {
      const number = Number(/^\[来源 (\d+)\]/.exec(entry.reference)?.[1]);
      if (!Number.isInteger(number)) throw new Error(`证据引用缺少编号: ${entry.reference}`);
      const bound = numbered.get(number);
      if (!bound) throw new Error(`证据引用不在本次注册表内: ${entry.reference}`);
      if (bound.sourceId !== entry.sourceId) {
        throw new Error(`证据引用编号指向其他来源: ${entry.reference} != ${entry.sourceId}`);
      }
    }
  }
  sourceIndex.sort((left, right) => left.number - right.number);

  const digest = {
    schemaVersion: DIGEST_SCHEMA_VERSION,
    kind: DIGEST_KIND,
    reportDate,
    dataDate,
    reportSha256,
    inputSha256,
    selectionSha256,
    // The presentation choice is captured at the morning input freeze. A
    // pre-upgrade archive has no such evidence and remains explicitly unknown.
    reportView: reportViewFromFrozenConfiguration(input.configuration),
    conclusionLines: [...(capture.conclusionLines ?? [])],
    facts: [...(capture.marketFacts ?? [])],
    items,
    sourceIndex,
    limitations,
    generatedAt: generatedAt ?? new Date().toISOString()
  };
  const body = `${canonicalJson(digest)}\n`;
  return { digest, body, digestSha256: sha256(body), compiledSha256: sha256(compiled) };
}
