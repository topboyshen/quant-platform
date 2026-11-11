import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

/* 测试夹具：写入一条「编排器已认领且已完成」的当日 occurrence，并写入把报告哈希与
   last-run 绑在一起的密封件。自动发送要求 occurrence 已完成（success + completedAt），
   且该运行开始/完成都落在认领时间窗内，因此夹具必须让二者一致。 */
export const SCHEDULED_INVOCATION = "6f1c2f0e-8b1a-4d5e-9c77-2a4b6d8e0f13";

const sha256 = (buffer) => createHash("sha256").update(buffer).digest("hex");

async function readJson(file) {
  return JSON.parse(await readFile(file, "utf8"));
}

function plusOneSecond(instant) {
  return new Date(Date.parse(instant) + 1000).toISOString();
}

export async function writeScheduledOccurrence(root, day, overrides = {}) {
  const runPath = path.join(root, "work", "last-run.json");
  let run = null;
  try {
    run = await readJson(runPath);
  } catch {
    run = null;
  }

  const states = path.join(root, "work", "orchestrator", "states");
  await mkdir(states, { recursive: true });
  const start = overrides.startedAt
    ?? run?.frozenCutoffInstant
    ?? `${day}T00:00:00.000Z`;
  const end = overrides.completedAt
    ?? (typeof (run?.completedAt ?? run?.failedAt) === "string"
      ? plusOneSecond(run.completedAt ?? run.failedAt) : `${day}T00:00:59.000Z`);
  const occurrence = {
    schemaVersion: "1.0",
    task: "morning-report",
    targetDate: day,
    status: ["failed", "blocked", "skipped"].includes(run?.status) ? run.status : "success",
    invocationId: SCHEDULED_INVOCATION,
    startedAt: start,
    completedAt: end,
    childExitCode: run?.status === "failed" ? 1 : run?.status === "blocked" ? 2 : 0,
    childSignal: null,
    timedOut: false,
    interrupted: false,
    ...overrides
  };
  await writeFile(path.join(states, `morning-report-${day}.json`),
    `${JSON.stringify(occurrence, null, 2)}\n`, "utf8");

  if (run && typeof run.reportFile === "string") {
    let reportBody = null;
    try {
      reportBody = await readFile(path.join(root, run.reportFile));
    } catch {
      reportBody = null;
    }
    if (reportBody) {
      const reportSha = sha256(reportBody);
      const runRaw = await readFile(runPath);
      const sealDir = path.join(root, "work", "research-seal", day);
      await mkdir(sealDir, { recursive: true });
      await writeFile(path.join(sealDir, `${reportSha}.json`), `${JSON.stringify({
        schemaVersion: "astock.research-seal.v1",
        kind: "research_seal",
        targetDate: day,
        state: "SEALED",
        generatedAt: `${day}T00:00:24.000Z`,
        source: {
          lastRunFile: "work/last-run.json",
          lastRunSha256: sha256(runRaw),
          reportFile: run.reportFile,
          reportSha256: reportSha,
          publicationGate: "passed",
          evidenceReview: "passed",
          frozenCutoffInstant: run.frozenCutoffInstant ?? start,
          contextSha256: run.contextSha256
        }
      }, null, 2)}\n`, "utf8");
    }
  }
  return occurrence;
}
