import test from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import {
  mkdir,
  mkdtemp,
  rm,
  symlink,
  writeFile
} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { promisify } from "node:util";

import { isMainEntry } from "../src/main-entry.mjs";

const execFileAsync = promisify(execFile);
const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("main entry recognizes a direct cross-platform file URL", () => {
  assert.equal(isMainEntry(import.meta.url, fileURLToPath(import.meta.url)), true);
});

test("main entry fails closed for absent and malformed paths", () => {
  const missingPath = path.join(os.tmpdir(), `astock-missing-${process.pid}-${Date.now()}.mjs`);
  assert.equal(isMainEntry(pathToFileURL(missingPath).href, missingPath), false);
  assert.equal(isMainEntry(import.meta.url, null), false);
  assert.equal(isMainEntry(import.meta.url, ""), false);
  assert.equal(isMainEntry("https://example.invalid/script.mjs", fileURLToPath(import.meta.url)), false);
});

test("main entry compares canonical paths through a symlink", {
  skip: process.platform === "win32" ? "directory symlink creation is not reliable on Windows CI" : false
}, async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "astock-main-entry-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const physicalDirectory = path.join(root, "physical");
  const linkedDirectory = path.join(root, "linked");
  await mkdir(physicalDirectory);
  const physicalFile = path.join(physicalDirectory, "entry.mjs");
  await writeFile(physicalFile, "// fixture\n", "utf8");
  await symlink(physicalDirectory, linkedDirectory, "dir");

  assert.equal(
    isMainEntry(pathToFileURL(physicalFile).href, path.join(linkedDirectory, "entry.mjs")),
    true
  );
});

test("all production CLIs execute when the project root is reached through a symlink", {
  skip: process.platform === "win32" ? "Linux deployment symlink integration" : false
}, async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "astock-cli-symlink-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const linkedProject = path.join(root, "astock-ai-system");
  await symlink(projectRoot, linkedProject, "dir");

  const orchestrator = await execFileAsync(
    process.execPath,
    [path.join(linkedProject, "scripts", "task-orchestrator.mjs"), "catalog"],
    { cwd: linkedProject }
  );
  assert.equal(JSON.parse(orchestrator.stdout).schemaVersion, "1.0");

  const invalidCases = [
    ["hermes-notify.mjs", "Hermes notification error:"],
    ["research-context.mjs", "research context failed:"],
    ["publish-correction.mjs", ""]
  ];
  for (const [script, stderrPrefix] of invalidCases) {
    await assert.rejects(
      execFileAsync(
        process.execPath,
        [path.join(linkedProject, "scripts", script), "--definitely-invalid"],
        { cwd: linkedProject }
      ),
      (error) => {
        assert.equal(error.code, 1);
        assert.notEqual(error.stderr, "");
        if (stderrPrefix) assert.match(error.stderr, new RegExp(`^${stderrPrefix}`));
        return true;
      }
    );
  }
});
