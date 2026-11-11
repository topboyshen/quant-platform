import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { access, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const wrapperPath = path.join(repositoryRoot, "scripts", "run-morning-cron.sh");
const bashPath = "/usr/bin/bash";

function run(command, args, options) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      ...options,
      shell: false,
      stdio: ["ignore", "pipe", "pipe"]
    });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.once("error", reject);
    child.once("close", (exitCode, signal) => {
      resolve({ exitCode, signal, stdout, stderr });
    });
  });
}

test("source wrapper keeps bounded retries and has no direct delivery capability", async () => {
  const source = await readFile(wrapperPath, "utf8");
  assert.equal((source.match(/hermes-notify\.mjs deliver/g) ?? []).length, 0);
  assert.equal((source.match(/scripts\/morning-report\.mjs/g) ?? []).length, 1);
  assert.equal((source.match(/scripts\/morning-retry-decision\.mjs/g) ?? []).length, 1);
  assert.match(source, /readonly MAX_ATTEMPTS=3/);
  assert.match(source, /readonly TOTAL_WORKFLOW_BUDGET_SECONDS=3300/);
  assert.match(source, /--target-date "\$target_date"/);
  assert.match(source, /--frozen-cutoff "\$frozen_cutoff_at"/);
  assert.match(source, /--frozen-context-sha256 "\$frozen_context_sha256"/);
  assert.match(source, /attempt_input="work\/morning-context\.json"/);
  assert.match(source, /frozen context could not be reused; stopping without recollection/);
  assert.match(source, /workflow_exit == 2/);
  assert.match(source, /workflow_exit == 124/);
  assert.match(source, /workflow_exit >= 128/);
  assert.match(source, /kill -s "\$signal_name" -- "\$workflow_pid"/);
  assert.match(source, /wait_for_workflow_exit 20/);
  assert.match(source, /ASTOCK_FINAL_DELIVERY:-disabled/);
  assert.match(source, /final_delivery" != "disabled/);
  assert.match(source, /legacy_wrapper_delivery_disabled/);
  assert.doesNotMatch(source, /deliver_final_notification\(\)|hermes-notify\.mjs|notification_exit/);
  assert.doesNotMatch(source, /content_gate_exhausted|report_publish_failed/);
});

test("source retries inside one invocation and never sends", async (t) => {
  if (process.platform === "win32") {
    t.skip("Linux cron wrapper integration");
    return;
  }
  try {
    await access(bashPath);
  } catch {
    t.skip("/usr/bin/bash unavailable");
    return;
  }

  const fixedHarness = path.join(
    repositoryRoot,
    "test",
    "morning-cron-retry-harness.sh"
  );
  const fixedResult = await run(bashPath, [fixedHarness, wrapperPath], {
    cwd: repositoryRoot,
    env: { ...process.env }
  });
  assert.equal(fixedResult.signal, null);
  assert.equal(fixedResult.exitCode, 0, fixedResult.stderr);
  assert.match(fixedResult.stdout, /morning cron retry integration: ok/);
});
