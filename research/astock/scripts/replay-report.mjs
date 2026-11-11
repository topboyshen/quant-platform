#!/usr/bin/env node
// Offline only: no provider, model, notifier, scheduler or relay is invoked.
import { readFile, realpath } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { loadProjectConfig } from "../src/config.mjs";
import { validateMorningContext } from "../src/data.mjs";
import { freezeRunInput, compileFrozenReport, sealRunResult, replayRunArchive, selectFrozenCandidates } from "../src/run-archive.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const [command, ...args] = process.argv.slice(2);
const options = {};
for (let index = 0; index < args.length; index += 1) {
  const key = args[index];
  if (key === "--select-all") { options.selectAll = true; continue; }
  if (!["--context", "--date", "--cutoff", "--archive-root", "--archive", "--document-store"].includes(key) || !args[index + 1]) {
    throw new Error(`unsupported replay option: ${key}`);
  }
  if (Object.hasOwn(options, key)) throw new Error(`duplicate replay option: ${key}`);
  options[key] = args[++index];
}

async function physicalDestination(directory) {
  try { return await realpath(directory); }
  catch (error) {
    if (error.code !== "ENOENT") throw error;
    return path.join(await physicalDestination(path.dirname(directory)), path.basename(directory));
  }
}

async function main() {
  if (command === "verify") {
    if (!options["--archive"]) throw new Error("verify requires --archive DIRECTORY");
    console.log(JSON.stringify(await replayRunArchive(root, path.resolve(options["--archive"])), null, 2));
    return;
  }
  if (command !== "create" || !options["--context"] || !options["--date"] || !options["--cutoff"] || !options["--archive-root"]) {
    throw new Error("Usage: replay-report.mjs create --context FILE --date YYYY-MM-DD --cutoff ISO --archive-root DIR [--select-all] [--document-store DIR] | verify --archive DIR");
  }
  const config = await loadProjectConfig(root);
  const rawContextText = await readFile(path.resolve(options["--context"]), "utf8");
  const rawContext = JSON.parse(rawContextText);
  validateMorningContext(rawContext, { targetDate: options["--date"], universe: config.stocks.corePool });
  const cutoff = new Date(options["--cutoff"]);
  const local = new Date(cutoff.getTime() + 8 * 3_600_000).toISOString().slice(0, 16).replace("T", " ");
  const archiveRoot = await physicalDestination(path.resolve(options["--archive-root"]));
  const productionArchive = await physicalDestination(path.resolve(root, config.workflow.workDirectory, "report-archives"));
  if (archiveRoot === productionArchive || archiveRoot.startsWith(`${productionArchive}${path.sep}`)) {
    throw new Error("historical validation must not write to production archives");
  }
  const frozen = await freezeRunInput({
    projectRoot: root, archiveRoot, outputDirectory: null, rawContext, rawContextText,
    configuration: { stocks: config.stocks, risk: config.risk, workflow: config.workflow },
    targetDate: options["--date"], cutoffInstant: cutoff, cutoffLocal: local, mode: "historical_validation",
    documentStore: options["--document-store"] ? path.resolve(options["--document-store"]) : null
  });
  const overlay = selectFrozenCandidates(frozen.input, options.selectAll ? frozen.input.candidateIds : []);
  const report = compileFrozenReport(frozen.input, overlay);
  await sealRunResult(frozen, { report, overlay, analysisStatus: "offline_validation" });
  const result = await replayRunArchive(root, frozen.archiveDirectory);
  console.log(JSON.stringify({ ...result, archiveDirectory: frozen.archiveDirectory, delivery: "disabled" }, null, 2));
}

main().catch((error) => { console.error(error.message); process.exitCode = 1; });
