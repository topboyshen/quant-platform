#!/usr/bin/env node
/**
 * Publish-branch gate CLI. Called by run-orchestrated-morning-cron.sh after a
 * zero-exit orchestrator run. Writes work/orchestrator/publish-branch.closed
 * only for a validated trading-calendar skip; never sends or edits reports.
 *
 *   node scripts/publish-branch-gate.mjs decide
 *   node scripts/publish-branch-gate.mjs decide --dry-run [--date YYYY-MM-DD]
 */
import path from "node:path";
import { fileURLToPath } from "node:url";

import { decidePublishBranch } from "../src/publish-branch.mjs";
import { isMainEntry } from "../src/main-entry.mjs";

const PROJECT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

export function parseGateArgs(argv) {
  if (argv[0] !== "decide") throw new Error("usage: publish-branch-gate.mjs decide [--dry-run [--date YYYY-MM-DD]]");
  const options = { dryRun: false, targetDate: null };
  for (let index = 1; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === "--dry-run") options.dryRun = true;
    else if (token === "--date") {
      const value = argv[index + 1];
      if (!/^\d{4}-\d{2}-\d{2}$/.test(value ?? "")) throw new Error("--date requires YYYY-MM-DD");
      options.targetDate = value;
      index += 1;
    } else throw new Error(`unknown argument: ${token}`);
  }
  // A historical date may only be inspected, never used to write a marker.
  if (options.targetDate !== null && !options.dryRun) throw new Error("--date is only allowed with --dry-run");
  return options;
}

export async function main(argv = process.argv.slice(2)) {
  let options;
  try {
    options = parseGateArgs(argv);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    return 2;
  }
  const result = await decidePublishBranch({ projectRoot: PROJECT_ROOT, ...options });
  process.stdout.write(`${JSON.stringify({ publishBranch: result.decision, ...result })}\n`);
  return 0;
}

if (isMainEntry(import.meta.url)) {
  main().then((code) => { process.exitCode = code; }).catch((error) => {
    process.stderr.write(`publish-branch gate failed: ${String(error?.message ?? error).slice(0, 200)}\n`);
    process.exitCode = 1;
  });
}
