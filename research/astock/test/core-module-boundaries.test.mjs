import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { promisify } from "node:util";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const exec = promisify(execFile);

// Follow static imports/re-exports and literal dynamic imports. The refactored
// production boundaries use no computed import paths.
async function localImports(relative) {
  const source = await readFile(path.join(root, relative), "utf8");
  const imports = [
    ...source.matchAll(/(?:import|export)\s+(?:[\s\S]*?\s+from\s*)?["']([^"']+)["']/g),
    ...source.matchAll(/import\(\s*["']([^"']+)["']\s*\)/g)
  ];
  return [...new Set(imports.map((match) => match[1])
    .filter((name) => name.startsWith("."))
    .map((name) => path.posix.normalize(path.posix.join(path.posix.dirname(relative), name))))];
}

async function dependencyClosure(entry, visiting = new Set(), seen = new Set()) {
  assert.equal(visiting.has(entry), false, `circular dependency at ${entry}`);
  if (seen.has(entry)) return seen;
  visiting.add(entry);
  for (const dependency of await localImports(entry)) await dependencyClosure(dependency, visiting, seen);
  visiting.delete(entry);
  seen.add(entry);
  return seen;
}

test("mandatory seal import graph excludes optional XHS and notification implementations", async () => {
  const graph = await dependencyClosure("scripts/research-seal.mjs");
  assert.ok(graph.has("src/research-seal.mjs"));
  assert.ok(graph.has("src/research-artifacts.mjs"));
  for (const name of graph) {
    // Frozen presentation is part of the digest contract, not a sender module.
    assert.doesNotMatch(name, /\/(?:xhs-draft|notification-|hermes-notify)/);
  }
});

test("notification dependencies are acyclic and normal delivery excludes manual recovery", async () => {
  await dependencyClosure("scripts/hermes-notify.mjs");
  const daily = await dependencyClosure("scripts/deliver-morning-report.mjs");
  assert.ok(daily.has("src/notification-queue.mjs"));
  assert.equal(daily.has("src/notification-recovery.mjs"), false);
  assert.equal(daily.has("scripts/hermes-notify.mjs"), false);
  const proof = await dependencyClosure("src/notification-proof.mjs");
  assert.equal(proof.has("src/notification-transport.mjs"), false);
  assert.equal(proof.has("src/notification-queue.mjs"), false);
  const transport = await localImports("src/notification-transport.mjs");
  assert.deepEqual(transport, []);
});

test("historical exports are the same functions and error class, not alternate implementations", async () => {
  const old = await import("../scripts/hermes-notify.mjs");
  const owners = {
    "notification-contract": ["validateDeliveryPolicy", "NOTIFICATION_ROOT"],
    "notification-proof": ["sentConfirmation"],
    "notification-receipts": ["publishSendReceipt", "validateSendReceipt", "readSendReceipt"],
    "notification-transport": ["confirmsHermesDelivery", "parseHermesAck", "sendViaHermes"],
    "notification-queue": ["emitRunEvent", "drainOutbox", "deliverRunEvent", "notificationStatus"],
    "notification-recovery": ["authorizeSend", "emitCorrectionEvent", "drainCorrectionEvent", "deliverCorrectionEvent"]
  };
  assert.deepEqual(Object.keys(old).sort(), [...Object.values(owners).flat(), "parseEmitArguments"].sort());
  for (const [module, names] of Object.entries(owners)) {
    const owner = await import(`../src/${module}.mjs`);
    for (const name of names) assert.equal(old[name], owner[name], name);
  }
  const draft = await import("../src/xhs-draft.mjs");
  const seal = await import("../src/research-seal.mjs");
  const shared = await import("../src/research-artifacts.mjs");
  assert.equal(draft.buildResearchSeal, seal.buildResearchSeal);
  assert.equal(draft.XhsDraftIntegrityError, shared.XhsDraftIntegrityError);
  const error = new shared.XhsDraftIntegrityError("FIXTURE", "fixture");
  assert.ok(error instanceof draft.XhsDraftIntegrityError);
  assert.equal(error.name, "XhsDraftIntegrityError");
});

test("importing core modules and compatibility CLIs cannot write, spawn or send", async () => {
  const entries = [
    "src/research-artifacts.mjs", "src/research-seal.mjs", "src/xhs-draft.mjs",
    "src/notification-contract.mjs", "src/notification-store.mjs", "src/notification-proof.mjs",
    "src/notification-receipts.mjs", "src/notification-transport.mjs", "src/notification-queue.mjs",
    "src/notification-recovery.mjs", "scripts/hermes-notify.mjs", "scripts/research-seal.mjs",
    "scripts/deliver-morning-report.mjs", "scripts/xhs-draft.mjs"
  ].map((name) => pathToFileURL(path.join(root, name)).href);
  const probe = `
    import childProcess from "node:child_process";
    import fs from "node:fs";
    import fsp from "node:fs/promises";
    import net from "node:net";
    import http from "node:http";
    import https from "node:https";
    import { syncBuiltinESMExports } from "node:module";
    const effects = [];
    const guard = (name) => () => { effects.push(name); throw new Error("import side effect: " + name); };
    for (const name of ["spawn", "spawnSync", "exec", "execSync", "execFile", "execFileSync", "fork"])
      childProcess[name] = guard(name);
    for (const name of ["writeFile", "appendFile", "mkdir", "rename", "unlink", "rm", "link", "chmod"])
      for (const [owner, key] of [[fsp, name], [fs, name], [fs, name + "Sync"]]) owner[key] = guard(key);
    net.connect = net.createConnection = guard("network");
    http.request = http.get = https.request = https.get = guard("http");
    globalThis.fetch = guard("fetch");
    syncBuiltinESMExports();
    for (const url of ${JSON.stringify(entries)}) await import(url);
    if (effects.length) throw new Error(effects.join(","));
    process.stdout.write("import-only: no side effects\\n");
  `;
  const result = await exec(process.execPath, ["--input-type=module", "-e", probe], {
    cwd: root, timeout: 10_000, maxBuffer: 64 * 1024
  });
  assert.equal(result.stdout, "import-only: no side effects\n");
  assert.equal(result.stderr, "");
});
