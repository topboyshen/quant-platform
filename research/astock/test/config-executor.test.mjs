import test from "node:test";
import assert from "node:assert/strict";

import { loadProjectConfig, validateAgentWorkflowConfig, validateProjectConfig } from "../src/config.mjs";
import { resolveAgentDescriptor } from "../src/agent-executor.mjs";
import { buildRunInstruction } from "../src/report.mjs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function validHermesWorkflow() {
  return {
    agentExecutor: "hermes",
    agentFallback: "none",
    reportMode: "structured_compiler",
    maxAnalysisAttempts: 2,
    scheduledAnalysisAttempts: 0,
    analysisTimeoutMs: 300_000,
    provider: "ollama-cloud",
    model: "deepseek-v4-pro",
    thinking: "high",
    maxAgentTurns: 40,
    piTimeoutMs: 900_000,
    reviewerTimeoutMs: 600_000,
    allowedTools: ["web_search", "source_check", "fetch_content", "get_search_content"],
    allowAgentWebFallback: false,
    hermesProvider: "kimi-coding",
    hermesModel: "k3-256k",
    hermesReasoning: "high",
    hermesMaxAgentTurns: 8,
    hermesReviewerMaxTurns: 5,
    hermesAnalysisMaxTurns: 5,
    hermesTimeoutMs: 900_000,
    hermesReviewerTimeoutMs: 600_000
  };
}

function validPiWorkflow() {
  return {
    ...validHermesWorkflow(),
    agentExecutor: "pi",
    allowAgentWebFallback: true
  };
}

function changed(workflow, field, value) {
  return { ...workflow, [field]: value };
}

test("facts keep the compiler, calendar, source and risk contract without validating model settings", async () => {
  const valid = await loadProjectConfig(projectRoot);
  const withBrokenModel = structuredClone(valid);
  Object.assign(withBrokenModel.workflow, {
    agentExecutor: "unknown", model: null, hermesModel: null,
    allowedTools: [], maxReviewAttempts: 0, analysisTimeoutMs: "bad"
  });
  assert.doesNotThrow(() => validateProjectConfig(withBrokenModel, { requireAgent: false }));
  assert.throws(() => validateProjectConfig(withBrokenModel), /agentExecutor/);
  for (const [field, value, reason] of [
    ["reportMode", "free_markdown", /reportMode/],
    ["timezone", "UTC", /时区/],
    ["bridgeTimeoutMs", 0, /bridgeTimeoutMs/],
    ["dataBridge", { executable: "/bin/sh", args: ["other.py"] }, /相对路径/]
  ]) {
    const brokenFacts = structuredClone(withBrokenModel);
    brokenFacts.workflow[field] = value;
    assert.throws(() => validateProjectConfig(brokenFacts, { requireAgent: false }), reason);
  }
  const brokenRisk = structuredClone(withBrokenModel);
  brokenRisk.risk.neverInventMissingData = false;
  assert.throws(() => validateProjectConfig(brokenRisk, { requireAgent: false }), /risk 配置/);
  const brokenPool = structuredClone(withBrokenModel);
  brokenPool.stocks.corePool = [];
  assert.throws(() => validateProjectConfig(brokenPool, { requireAgent: false }), /corePool/);
});

test("accepts valid Hermes and Pi executor configurations", () => {
  assert.doesNotThrow(() => validateAgentWorkflowConfig(validHermesWorkflow()));
  assert.doesNotThrow(() => validateAgentWorkflowConfig(validPiWorkflow()));
});

test("runtime instruction records the selected Hermes backend rather than the Pi rollback model", () => {
  const workflow = { ...validHermesWorkflow(), timezone: "Asia/Shanghai" };
  const instruction = buildRunInstruction({
    targetDate: "2026-08-04",
    workflow,
    agentDescriptor: resolveAgentDescriptor(workflow),
    loadedFrom: "fixture",
    dataStatus: "partial",
    disclaimer: "test disclaimer",
    cutoffLocal: "2026-08-04 08:00",
    cutoffIso: "2026-08-04T00:00:00.000Z",
    requireStaleMarketLabel: false,
    marketDataMaxAgeHours: 18,
    newsMaxAgeHours: 72,
    publicationLevel: "草稿（测试）"
  });
  assert.match(instruction, /固定执行器：hermes/);
  assert.match(instruction, /固定模型：kimi-coding\/k3-256k/);
  assert.doesNotMatch(instruction, /ollama-cloud\/deepseek-v4-pro/);
});

test("rejects missing or unknown executors", () => {
  const missing = validHermesWorkflow();
  delete missing.agentExecutor;

  assert.throws(() => validateAgentWorkflowConfig(missing), /agentExecutor/);
  assert.throws(
    () => validateAgentWorkflowConfig(changed(validHermesWorkflow(), "agentExecutor", "other")),
    /agentExecutor/
  );
});

test("rejects every fallback mode except none", () => {
  for (const value of [undefined, null, "pi", "hermes", true]) {
    assert.throws(
      () => validateAgentWorkflowConfig(changed(validHermesWorkflow(), "agentFallback", value)),
      /agentFallback/
    );
  }
});

test("pins structured compiler mode and bounded analysis attempts", () => {
  for (const value of [undefined, null, "free_markdown", true]) {
    assert.throws(
      () => validateAgentWorkflowConfig(changed(validHermesWorkflow(), "reportMode", value)),
      /reportMode/
    );
  }
  for (const value of [undefined, 0, 3, 1.5]) {
    assert.throws(
      () => validateAgentWorkflowConfig(changed(validHermesWorkflow(), "maxAnalysisAttempts", value)),
      /maxAnalysisAttempts/
    );
  }
  for (const value of [undefined, -1, 3, 1.5]) {
    assert.throws(
      () => validateAgentWorkflowConfig(changed(validHermesWorkflow(), "scheduledAnalysisAttempts", value)),
      /scheduledAnalysisAttempts/
    );
  }
  assert.doesNotThrow(() => validateAgentWorkflowConfig(changed(validHermesWorkflow(), "scheduledAnalysisAttempts", 2)));
  for (const value of [undefined, 999, 600_001, 1.5]) {
    assert.throws(
      () => validateAgentWorkflowConfig(changed(validHermesWorkflow(), "analysisTimeoutMs", value)),
      /analysisTimeoutMs/
    );
  }
  for (const value of [undefined, 0, 6, 1.5]) {
    assert.throws(
      () => validateAgentWorkflowConfig(changed(validHermesWorkflow(), "hermesAnalysisMaxTurns", value)),
      /hermesAnalysisMaxTurns/
    );
  }
});

test("rejects an unpinned Hermes provider, model, or reasoning level", () => {
  for (const [field, value, pattern] of [
    ["hermesProvider", "other-provider", /Hermes/],
    ["hermesModel", "other-model", /Hermes/],
    ["hermesReasoning", "medium", /hermesReasoning/]
  ]) {
    assert.throws(
      () => validateAgentWorkflowConfig(changed(validHermesWorkflow(), field, value)),
      pattern
    );
  }
});

test("rejects Hermes turn limits outside the closed 1..12 range", () => {
  for (const field of ["hermesMaxAgentTurns", "hermesReviewerMaxTurns"]) {
    for (const value of [undefined, 0, 13, 1.5]) {
      assert.throws(
        () => validateAgentWorkflowConfig(changed(validHermesWorkflow(), field, value)),
        new RegExp(field)
      );
    }
  }
});

test("rejects Hermes timeouts outside the closed 1000..1800000 range", () => {
  for (const field of ["hermesTimeoutMs", "hermesReviewerTimeoutMs"]) {
    for (const value of [undefined, 999, 1_800_001, 1_000.5]) {
      assert.throws(
        () => validateAgentWorkflowConfig(changed(validHermesWorkflow(), field, value)),
        new RegExp(field)
      );
    }
  }
});

test("Hermes must not enable agent-side web fallback", () => {
  assert.throws(
    () => validateAgentWorkflowConfig(changed(validHermesWorkflow(), "allowAgentWebFallback", true)),
    /Hermes/
  );
});

test("rejects changes to the pinned Pi rollback provider and model", () => {
  for (const [field, value] of [
    ["provider", "other-provider"],
    ["model", "other-model"]
  ]) {
    assert.throws(
      () => validateAgentWorkflowConfig(changed(validPiWorkflow(), field, value)),
      /Pi/
    );
  }
});

test("rejects invalid Pi rollback fields", () => {
  const invalidCases = [
    ["thinking", "ultra", /Pi thinking/],
    ["maxAgentTurns", 4, /maxAgentTurns/],
    ["maxAgentTurns", 101, /maxAgentTurns/],
    ["piTimeoutMs", 999, /piTimeoutMs/],
    ["reviewerTimeoutMs", 1_800_001, /reviewerTimeoutMs/],
    ["allowedTools", [], /allowedTools/],
    ["allowedTools", ["terminal"], /allowedTools/],
    ["allowedTools", ["web_search", "web_search"], /allowedTools/],
    ["allowAgentWebFallback", "false", /allowAgentWebFallback/]
  ];

  for (const [field, value, pattern] of invalidCases) {
    assert.throws(
      () => validateAgentWorkflowConfig(changed(validPiWorkflow(), field, value)),
      pattern,
      `${field}=${JSON.stringify(value)} should be rejected`
    );
  }
});
