/**
 * Research-seal regression tests: the seal must validate date/gates/hashes and
 * the numbered-source contract, and must be produced without any XHS input.
 */
import assert from "node:assert/strict";
import { access, mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { jsonHash } from "../src/snapshot-integrity.mjs";
import { buildResearchSeal, XhsDraftIntegrityError } from "../src/xhs-draft.mjs";
import { parseResearchSealArgs } from "../scripts/research-seal.mjs";
import {
  CNINFO_SOURCE_FIXED,
  CNINFO_SOURCE_UNPARSABLE,
  sha256,
  writeFixture
} from "./helpers/research-seal-fixture.mjs";

const TARGET_DATE = "2026-09-21";
const MARKET_DATE = "2026-09-18";
const NOW = new Date("2026-09-21T00:20:00.000Z");

async function fixture(options = {}) {
  const root = await mkdtemp(path.join(os.tmpdir(), "astock-research-seal-"));
  const written = await writeFixture(root, { targetDate: TARGET_DATE, marketDate: MARKET_DATE, ...options });
  return { root, ...written };
}

async function exists(target) {
  try {
    await access(target);
    return true;
  } catch {
    return false;
  }
}

test("seals validated research without reading any XHS input", async () => {
  const { root, report } = await fixture();
  try {
    const result = await buildResearchSeal({
      projectRoot: root,
      requireCurrentDate: true,
      maxAgeMinutes: 120,
      now: NOW
    });
    assert.equal(result.status, "sealed");
    assert.equal(result.targetDate, TARGET_DATE);
    assert.equal(result.sealFile, `work/research-seal/${TARGET_DATE}/${sha256(report)}.json`);
    const seal = JSON.parse(await readFile(path.join(root, result.sealFile), "utf8"));
    assert.equal(seal.schemaVersion, "astock.research-seal.v1");
    assert.equal(seal.kind, "research_seal");
    assert.equal(seal.state, "SEALED");
    assert.equal(seal.targetDate, TARGET_DATE);
    assert.equal(seal.source.reportSha256, sha256(report));
    assert.equal(seal.source.reportFile, `reports/daily/${TARGET_DATE}.md`);
    assert.equal(seal.source.dataStatus, "partial");
    assert.equal(seal.source.researchSemanticReview, "not_human_reviewed");
    assert.equal(seal.source.publicationGate, "passed");
    assert.equal(seal.source.evidenceReview, "passed");
    assert.equal(seal.source.cutoffLocal, `${TARGET_DATE} 08:00`);
    assert.equal(seal.source.lastRunFile, "work/last-run.json");
    assert.match(seal.source.lastRunSha256, /^[0-9a-f]{64}$/);
    assert.match(seal.source.contextSha256, /^[0-9a-f]{64}$/);
    // No XHS draft, no XHS config: the research seal must not need either.
    assert.equal(await exists(path.join(root, "work", "xhs")), false);
    assert.equal(await exists(path.join(root, "config", "xhs.json")), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("re-sealing identical research is a noop and never rewrites the seal", async () => {
  const { root } = await fixture();
  try {
    const first = await buildResearchSeal({ projectRoot: root, now: NOW });
    assert.equal(first.status, "sealed");
    const sealPath = path.join(root, first.sealFile);
    const before = await readFile(sealPath, "utf8");
    const second = await buildResearchSeal({ projectRoot: root, now: NOW });
    assert.equal(second.status, "noop");
    assert.equal(second.sealFile, first.sealFile);
    assert.equal(await readFile(sealPath, "utf8"), before);
    const reportBody = await readFile(path.join(root, "reports", "daily", `${TARGET_DATE}.md`), "utf8");
    const reportSha = sha256(reportBody);
    assert.deepEqual(await readdir(path.join(root, "work", "research-seal", TARGET_DATE)),
      [`${reportSha}.digest.json`, `${reportSha}.json`]);
    // The digest is declared by the manifest and its bytes match that declaration.
    const seal = JSON.parse(before);
    assert.equal(seal.source.digestFile, `work/research-seal/${TARGET_DATE}/${reportSha}.digest.json`);
    assert.equal(seal.source.digestSha256,
      sha256(await readFile(path.join(root, "work", "research-seal", TARGET_DATE, `${reportSha}.digest.json`))));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("an unresolvable numbered source is never sealed (2026-09-17 regression)", async () => {
  const { root } = await fixture({ sourceTwo: CNINFO_SOURCE_UNPARSABLE, reportMode: "synthetic" });
  try {
    await assert.rejects(
      buildResearchSeal({ projectRoot: root, now: NOW }),
      (error) => error instanceof XhsDraftIntegrityError
        && error.code === "REPORT_CONTRACT_MISMATCH"
    );
    assert.equal(await exists(path.join(root, "work", "research-seal")), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("the CNINFO disclosure-date source shape is accepted for sealing", async () => {
  const { root } = await fixture({ sourceTwo: CNINFO_SOURCE_FIXED, reportMode: "synthetic" });
  try {
    const result = await buildResearchSeal({ projectRoot: root, now: NOW });
    // The report contract accepted the CNINFO shape. R05 then refuses to bind a
    // digest to a body that is not this compilation's own output, so a synthetic
    // body stops at the digest step instead of being sealed.
    assert.equal(result.status, "held");
    assert.deepEqual(result.reasonCodes, ["DIGEST_REPORT_RECOMPILE_MISMATCH"]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("stale or foreign research is held without a seal", async () => {
  const { root } = await fixture({ targetDate: "2026-09-18", marketDate: "2026-09-17" });
  try {
    const result = await buildResearchSeal({
      projectRoot: root,
      requireCurrentDate: true,
      maxAgeMinutes: 120,
      now: NOW
    });
    assert.equal(result.status, "held");
    // Both the date mismatch and the age window legitimately apply here.
    assert.ok(result.reasonCodes.includes("NOT_CURRENT_SHANGHAI_DATE"), result.reasonCodes);
    assert.ok(result.reasonCodes.includes("REPORT_TOO_OLD"), result.reasonCodes);
    assert.equal(await exists(path.join(root, "work", "research-seal")), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("a failed publication gate is held without a seal", async () => {
  const { root } = await fixture({ lastRunPatch: { publicationGate: "blocked" } });
  try {
    const result = await buildResearchSeal({ projectRoot: root, now: NOW });
    assert.equal(result.status, "held");
    assert.deepEqual(result.reasonCodes, ["PUBLICATION_GATE_NOT_PASSED"]);
    assert.equal(await exists(path.join(root, "work", "research-seal")), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("dry-run reports the target seal path without writing it", async () => {
  const { root, report } = await fixture();
  try {
    const result = await buildResearchSeal({ projectRoot: root, dryRun: true, now: NOW });
    assert.equal(result.status, "ready");
    assert.equal(result.sealFile, `work/research-seal/${TARGET_DATE}/${sha256(report)}.json`);
    assert.equal(await exists(path.join(root, "work", "research-seal")), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("CLI argument parsing rejects unknown flags and non-integer age", () => {
  assert.deepEqual(parseResearchSealArgs(["--dry-run"]),
    { command: "seal", desiredDate: null, requireCurrentDate: false, maxAgeMinutes: null, dryRun: true });
  assert.deepEqual(parseResearchSealArgs(["--date", TARGET_DATE, "--max-age-minutes", "120"]),
    { command: "seal", desiredDate: TARGET_DATE, requireCurrentDate: false, maxAgeMinutes: 120, dryRun: false });
  assert.equal(parseResearchSealArgs(["--help"]).command, "help");
  assert.throws(() => parseResearchSealArgs(["--nope"]));
  assert.throws(() => parseResearchSealArgs(["--max-age-minutes", "abc"]));
  assert.throws(() => parseResearchSealArgs(["--date"]));
});


test("R05: a body that is not this compilation's output is never sealed", async () => {
  const { root, report } = await fixture();
  try {
    const archive = path.join(root, "work/report-archives", TARGET_DATE);
    // Bind the archive to a different body: the digest's own compilation no longer
    // reproduces the sealed report, so the seal must hold instead of claiming it.
    const other = `${report}\n未核验的额外正文\n`;
    await writeFile(path.join(archive, "result/report.md"), other);
    const result = await buildResearchSeal({ projectRoot: root, now: NOW });
    assert.equal(result.status, "held");
    assert.deepEqual(result.reasonCodes, ["ARCHIVE_REPORT_HASH_MISMATCH"]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("R05: the archive publication identity is reused, not just the result binding", async () => {
  const { root } = await fixture();
  try {
    const publishedPath = path.join(root, "work/report-archives", TARGET_DATE, "published.json");
    const published = JSON.parse(await readFile(publishedPath, "utf8"));
    published.status = "failed";
    await writeFile(publishedPath, `${JSON.stringify(published)}\n`);
    const failed = await buildResearchSeal({ projectRoot: root, now: NOW });
    assert.equal(failed.status, "held");
    assert.deepEqual(failed.reasonCodes, ["ARCHIVE_PUBLICATION_IDENTITY_MISMATCH"]);

    await rm(publishedPath);
    const missing = await buildResearchSeal({ projectRoot: root, now: NOW });
    assert.equal(missing.status, "held");
    assert.deepEqual(missing.reasonCodes, ["ARCHIVE_PUBLICATION_MISSING"]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("R06/R10: a damaged existing seal is neither noop nor ready in dry-run", async () => {
  const { root } = await fixture();
  try {
    const first = await buildResearchSeal({ projectRoot: root, now: NOW });
    assert.equal(first.status, "sealed");
    const sealPath = path.join(root, first.sealFile);
    const seal = JSON.parse(await readFile(sealPath, "utf8"));
    seal.state = "REJECTED";
    seal.targetDate = "2026-09-19";
    seal.source.reportSha256 = "0".repeat(64);
    seal.source.digestFile = "work/research-seal/2026-09-21/other.digest.json";
    await writeFile(sealPath, `${JSON.stringify(seal)}\n`);

    const dry = await buildResearchSeal({ projectRoot: root, now: NOW, dryRun: true });
    assert.equal(dry.status, "held", JSON.stringify(dry));
    assert.deepEqual(dry.reasonCodes, ["EXISTING_SEAL_IDENTITY_MISMATCH"]);
    const real = await buildResearchSeal({ projectRoot: root, now: NOW });
    assert.equal(real.status, "held");
    assert.deepEqual(real.reasonCodes, ["EXISTING_SEAL_IDENTITY_MISMATCH"]);
    // The damaged manifest is never rewritten by either path.
    assert.equal(JSON.parse(await readFile(sealPath, "utf8")).state, "REJECTED");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("R06: a deleted digest still holds for both dry-run and the real path", async () => {
  const { root, report } = await fixture();
  try {
    const first = await buildResearchSeal({ projectRoot: root, now: NOW });
    assert.equal(first.status, "sealed");
    const digestPath = path.join(root, "work/research-seal", TARGET_DATE, `${sha256(report)}.digest.json`);
    await rm(digestPath);
    const dry = await buildResearchSeal({ projectRoot: root, now: NOW, dryRun: true });
    assert.equal(dry.status, "held");
    assert.deepEqual(dry.reasonCodes, ["DIGEST_MISSING_AFTER_SEAL"]);
    assert.equal((await buildResearchSeal({ projectRoot: root, now: NOW })).status, "held");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});


test("R05: a wrong compilationInputSha256 is rejected like the strict reader does", async () => {
  const { root } = await fixture();
  try {
    const resultPath = path.join(root, "work/report-archives", TARGET_DATE, "result/result.json");
    const result = JSON.parse(await readFile(resultPath, "utf8"));
    result.compilationInputSha256 = "0".repeat(64);
    await writeFile(resultPath, `${JSON.stringify(result)}\n`);
    const publishedPath = path.join(root, "work/report-archives", TARGET_DATE, "published.json");
    const published = JSON.parse(await readFile(publishedPath, "utf8"));
    published.resultSha256 = sha256(`${JSON.stringify(result)}\n`);
    await writeFile(publishedPath, `${JSON.stringify(published)}\n`);
    const seal = await buildResearchSeal({ projectRoot: root, now: NOW });
    assert.equal(seal.status, "held");
    assert.deepEqual(seal.reasonCodes, ["ARCHIVE_COMPILATION_INPUT_MISMATCH"]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("R06: each manifest identity field is checked independently in both paths", async () => {
  const { root } = await fixture();
  try {
    const first = await buildResearchSeal({ projectRoot: root, now: NOW });
    assert.equal(first.status, "sealed");
    const sealPath = path.join(root, first.sealFile);
    const original = JSON.parse(await readFile(sealPath, "utf8"));
    const cases = [
      ...Object.keys(original).filter((field) => field !== "source").map((field) => [null, field]),
      // digestSha256 is checked against actual bytes separately. Every other
      // declaration must reject on its own, starting from the untouched seal.
      ...Object.keys(original.source).filter((field) => field !== "digestSha256").map((field) => ["source", field]),
      [null, "undeclaredField"], ["source", "undeclaredField"]
    ];
    for (const [scope, field] of cases) {
      const seal = structuredClone(original);
      const object = scope ? seal[scope] : seal;
      object[field] = `${object[field] ?? ""}:tampered`;
      const body = `${JSON.stringify(seal, null, 2)}\n`;
      await writeFile(sealPath, body);
      for (const dryRun of [true, false]) {
        const again = await buildResearchSeal({ projectRoot: root, now: NOW, dryRun });
        const label = `${scope ?? "seal"}.${field} dryRun=${dryRun}`;
        assert.equal(again.status, "held", label);
        assert.deepEqual(again.reasonCodes, ["EXISTING_SEAL_IDENTITY_MISMATCH"], label);
        assert.equal(await readFile(sealPath, "utf8"), body, `${label}: evidence must remain unchanged`);
      }
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("R06: tampering only seal.generatedAt preserves the digest but blocks both paths", async () => {
  const { root } = await fixture();
  try {
    const first = await buildResearchSeal({ projectRoot: root, now: NOW });
    assert.equal(first.status, "sealed");
    const sealPath = path.join(root, first.sealFile);
    const seal = JSON.parse(await readFile(sealPath, "utf8"));
    const digestPath = path.join(root, seal.source.digestFile);
    const digestBefore = await readFile(digestPath);
    const originalDigestSha256 = seal.source.digestSha256;
    seal.generatedAt = "2099-01-01T00:00:00.000Z";
    const body = `${JSON.stringify(seal, null, 2)}\n`;
    await writeFile(sealPath, body);
    for (const dryRun of [true, false]) {
      const result = await buildResearchSeal({ projectRoot: root, now: NOW, dryRun });
      assert.equal(result.status, "held");
      assert.deepEqual(result.reasonCodes, ["EXISTING_SEAL_IDENTITY_MISMATCH"]);
      assert.equal(await readFile(sealPath, "utf8"), body);
      assert.deepEqual(await readFile(digestPath), digestBefore);
      assert.equal(sha256(await readFile(digestPath)), originalDigestSha256);
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("R10: a conflicting digest without a seal is not ready in dry-run", async () => {
  const { root, report } = await fixture();
  try {
    const digestPath = path.join(root, "work/research-seal", TARGET_DATE, `${sha256(report)}.digest.json`);
    await mkdir(path.dirname(digestPath), { recursive: true });
    await writeFile(digestPath, '{"schemaVersion":"tampered"}\n');
    const dry = await buildResearchSeal({ projectRoot: root, now: NOW, dryRun: true });
    assert.equal(dry.status, "held", JSON.stringify(dry));
    assert.deepEqual(dry.reasonCodes, ["DIGEST_IMMUTABLE_CONFLICT"]);
    const real = await buildResearchSeal({ projectRoot: root, now: NOW });
    assert.equal(real.status, "held");
    assert.deepEqual(real.reasonCodes, ["DIGEST_IMMUTABLE_CONFLICT"]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});


test("R06: tampering only generatedAt (with its declared hash) still holds", async () => {
  const { root, report } = await fixture();
  try {
    const first = await buildResearchSeal({ projectRoot: root, now: NOW });
    assert.equal(first.status, "sealed");
    const sealPath = path.join(root, first.sealFile);
    const digestPath = path.join(root, "work/research-seal", TARGET_DATE, `${sha256(report)}.digest.json`);
    const digest = JSON.parse(await readFile(digestPath, "utf8"));
    digest.generatedAt = "2026-09-21T00:00:00.000Z";
    const body = `${JSON.stringify(digest, null, 2)}\n`;
    await writeFile(digestPath, body);
    // 同步清单声明，隔离“字段绑定”检查本身（否则先命中摘要哈希不符）
    const seal = JSON.parse(await readFile(sealPath, "utf8"));
    seal.source.digestSha256 = sha256(body);
    await writeFile(sealPath, `${JSON.stringify(seal)}\n`);

    const again = await buildResearchSeal({ projectRoot: root, now: NOW });
    assert.equal(again.status, "held", JSON.stringify(again));
    assert.deepEqual(again.reasonCodes, ["EXISTING_DIGEST_BINDING_MISMATCH"]);
    const dry = await buildResearchSeal({ projectRoot: root, now: NOW, dryRun: true });
    assert.equal(dry.status, "held");
    assert.deepEqual(dry.reasonCodes, ["EXISTING_DIGEST_BINDING_MISMATCH"]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("R06: tampering only generatedAt without syncing the hash still holds", async () => {
  const { root, report } = await fixture();
  try {
    const first = await buildResearchSeal({ projectRoot: root, now: NOW });
    assert.equal(first.status, "sealed");
    const digestPath = path.join(root, "work/research-seal", TARGET_DATE, `${sha256(report)}.digest.json`);
    const digest = JSON.parse(await readFile(digestPath, "utf8"));
    digest.generatedAt = "2026-09-21T00:00:00.000Z";
    await writeFile(digestPath, `${JSON.stringify(digest, null, 2)}\n`);
    const again = await buildResearchSeal({ projectRoot: root, now: NOW });
    assert.equal(again.status, "held");
    assert.deepEqual(again.reasonCodes, ["DIGEST_HASH_MISMATCH_AFTER_SEAL"]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("internal context hash mismatch is held even after sidecar rewrite", async () => {
  const { root } = await fixture();
  try {
    const archive = path.join(root, "work/report-archives", TARGET_DATE);
    const inputPath = path.join(archive, "input.json");
    const input = JSON.parse(await readFile(inputPath, "utf8"));
    input.compilerContextSha256 = "0".repeat(64);
    const inputBody = `${JSON.stringify(input)}\n`;
    await writeFile(inputPath, inputBody);
    const inputSha256 = sha256(inputBody);
    await writeFile(path.join(archive, "input.sha256"), `${inputSha256}\n`);
    const resultPath = path.join(archive, "result/result.json");
    const result = JSON.parse(await readFile(resultPath, "utf8"));
    result.inputSha256 = inputSha256;
    result.compilationInputSha256 = jsonHash({ inputSha256, selection: result.selection ?? null });
    await writeFile(resultPath, `${JSON.stringify(result)}\n`);
    const publishedPath = path.join(archive, "published.json");
    const published = JSON.parse(await readFile(publishedPath, "utf8"));
    published.inputSha256 = inputSha256;
    published.resultSha256 = jsonHash(result);
    await writeFile(publishedPath, `${JSON.stringify(published)}\n`);
    const seal = await buildResearchSeal({ projectRoot: root, now: NOW });
    assert.equal(seal.status, "held", JSON.stringify(seal));
    assert.deepEqual(seal.reasonCodes, ["ARCHIVE_CONTEXT_HASH_MISMATCH"]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

async function rewriteArchiveInput(root, mutate) {
  const archive = path.join(root, "work/report-archives", TARGET_DATE);
  const inputPath = path.join(archive, "input.json");
  const input = JSON.parse(await readFile(inputPath, "utf8"));
  mutate(input);
  const inputBody = `${JSON.stringify(input)}\n`;
  await writeFile(inputPath, inputBody);
  const inputSha256 = sha256(inputBody);
  await writeFile(path.join(archive, "input.sha256"), `${inputSha256}\n`);
  const resultPath = path.join(archive, "result/result.json");
  const result = JSON.parse(await readFile(resultPath, "utf8"));
  result.inputSha256 = inputSha256;
  result.compilationInputSha256 = jsonHash({ inputSha256, selection: result.selection ?? null });
  await writeFile(resultPath, `${JSON.stringify(result)}\n`);
  const publishedPath = path.join(archive, "published.json");
  const published = JSON.parse(await readFile(publishedPath, "utf8"));
  published.inputSha256 = inputSha256;
  published.resultSha256 = jsonHash(result);
  await writeFile(publishedPath, `${JSON.stringify(published)}\n`);
}

test("non-derivable compiler context is held by the strict reader", async () => {
  const { root } = await fixture();
  try {
    await rewriteArchiveInput(root, (input) => {
      input.compilerContext = { ...input.compilerContext, injected: "not-from-raw" };
      input.compilerContextSha256 = jsonHash(input.compilerContext);
    });
    const seal = await buildResearchSeal({ projectRoot: root, now: NOW });
    assert.equal(seal.status, "held", JSON.stringify(seal));
    assert.deepEqual(seal.reasonCodes, ["ARCHIVE_STRICT_READ_FAILED"]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("missing compiler code fingerprint is held", async () => {
  const { root } = await fixture();
  try {
    await rewriteArchiveInput(root, (input) => {
      if (input.code && typeof input.code === "object") delete input.code.filesSha256;
    });
    const seal = await buildResearchSeal({ projectRoot: root, now: NOW });
    assert.equal(seal.status, "held", JSON.stringify(seal));
    assert.deepEqual(seal.reasonCodes, ["ARCHIVE_CODE_FINGERPRINT_MISSING"]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("contradictory document manifest is held by the strict reader", async () => {
  const { root } = await fixture();
  try {
    await rewriteArchiveInput(root, (input) => {
      input.sourceDocuments = [...(input.sourceDocuments ?? []), {
        sha256: "f".repeat(64), title: "contradictory", bytes: 1
      }];
    });
    const seal = await buildResearchSeal({ projectRoot: root, now: NOW });
    assert.equal(seal.status, "held", JSON.stringify(seal));
    assert.deepEqual(seal.reasonCodes, ["ARCHIVE_STRICT_READ_FAILED"]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

for (const [name, mutate] of [
  ["fingerprint differs from its file manifest", (input) => { input.code.filesSha256 = "0".repeat(64); }],
  ["current fingerprint conceals a changed file manifest", (input) => { input.code.files["src/digest-view.mjs"] = "f".repeat(64); }],
  ["self-consistent different code era", (input) => {
    input.code.files["src/digest-view.mjs"] = "f".repeat(64);
    input.code.filesSha256 = jsonHash(input.code.files);
  }]
]) {
  test(`new digest rejects ${name} before any output`, async () => {
    const { root } = await fixture();
    try {
      await rewriteArchiveInput(root, mutate);
      for (const dryRun of [true, false]) {
        const result = await buildResearchSeal({ projectRoot: root, now: NOW, dryRun });
        assert.equal(result.status, "held");
        assert.deepEqual(result.reasonCodes, ["ARCHIVE_STRICT_READ_FAILED"]);
        assert.equal(await exists(path.join(root, "work/research-seal")), false);
      }
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });
}
