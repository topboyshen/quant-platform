import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("scheduled owner records lock conflicts and leaves delivery to the sealed-report entry", async () => {
  const outer = await readFile(
    path.join(projectRoot, "scripts", "run-orchestrated-morning-cron.sh"),
    "utf8"
  );
  const inner = await readFile(
    path.join(projectRoot, "scripts", "run-morning-cron.sh"),
    "utf8"
  );

  assert.match(outer, /\/usr\/bin\/flock/);
  assert.match(outer, /\/usr\/bin\/realpath -e -- "\$\{BASH_SOURCE\[0\]\}"/);
  assert.match(outer, /PROJECT_ROOT="\$\(\/usr\/bin\/realpath -e --/);
  assert.doesNotMatch(outer, /PROJECT_ROOT="\$\(cd /);
  assert.match(outer, /exec 9>>"\$LOCK_FILE"/);
  assert.match(outer, /flock --exclusive --nonblock 9/);
  assert.match(outer, /morning-report lock conflict/);
  assert.match(outer, /record-lock-conflict morning-report/);
  assert.match(outer, /exit 75/);
  assert.match(outer, /task-orchestrator\.mjs/);
  assert.doesNotMatch(outer, /hermes-notify\.mjs/);
  assert.doesNotMatch(outer, /morning-report\.mjs/);

  assert.match(inner, /scripts\/morning-report\.mjs[\s\S]*--strict-data[\s\S]*--trading-days-only/);
  assert.match(inner, /trap handle_term TERM/);
  assert.match(inner, /kill -s "\$signal_name" -- "-\$workflow_pid"/);
  assert.match(inner, /kill -s "\$signal_name" -- "\$workflow_pid"/);
  assert.match(inner, /signal_workflow_group KILL/);
  assert.match(inner, /wait_for_workflow_exit 20/);
  assert.match(inner, /if \(\( interrupted_exit != 0 \)\); then[\s\S]*return "\$interrupted_exit"/);
  assert.equal((inner.match(/hermes-notify\.mjs deliver/g) ?? []).length, 0);
  assert.equal((inner.match(/hermes-notify\.mjs emit/g) ?? []).length, 0);
  assert.equal((inner.match(/hermes-notify\.mjs drain/g) ?? []).length, 0);
  assert.match(inner, /legacy_wrapper_delivery_disabled/);
  assert.match(inner, /scripts\/deliver-morning-report\.mjs/);
});

test("documented code-only deployment includes every required agent prompt", async () => {
  const deployment = await readFile(path.join(projectRoot, "DEPLOYMENT.md"), "utf8");
  assert.match(deployment, /scripts mcp src config agents test package\.json/);
});

test("Pi research extension reaches evidence only through the fixed orchestrator route", async () => {
  const extension = await readFile(
    path.join(projectRoot, ".pi", "extensions", "astock-research.ts"),
    "utf8"
  ).catch(() => readFile(path.join(projectRoot, ".pi", "astock-research.ts"), "utf8"));
  assert.match(extension, /task-orchestrator\.mjs/);
  assert.match(extension, /\[ORCHESTRATOR, "run", "research-context"\]/);
  assert.doesNotMatch(extension, /const SCRIPT = .*research-context\.mjs/);
  assert.doesNotMatch(extension, /shell:\s*true/);
});
