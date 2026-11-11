import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { access, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const scripts = [
  "scripts/run-morning-watchdog.sh",
  "scripts/install-morning-schedule.sh",
  "scripts/repair-morning-schedule.sh"
];

for (const relative of scripts) {
  test(`${relative} has valid bash syntax`, async (t) => {
    await access(path.join(root, relative));
    const probe = spawnSync("bash", ["-n", path.join(root, relative)], { encoding: "utf8" });
    if (probe.error?.code === "ENOENT") t.skip("bash is unavailable on this platform");
    assert.equal(probe.status, 0, probe.stderr);
  });
}

test("retired watchdog cannot execute catch-up even when state is absent", async () => {
  const source = await readFile(path.join(root, "scripts/run-morning-watchdog.sh"), "utf8");
  assert.match(source, /legacy_watchdog_disabled/);
  assert.match(source, /user astock-xhs-source.timer/);
  assert.doesNotMatch(source, /exec |crontab|systemctl|sudo|NODE_BIN|read_state|\$RUNNER/);
  for (const args of [[], ["--catch-up"], ["--manual-same-day"]]) {
    const result = spawnSync("/bin/bash", [path.join(root, "scripts/run-morning-watchdog.sh"), ...args], {
      env: { PATH: "/nonexistent", ASTOCK_TARGET_DATE: "2026-09-29" }, encoding: "utf8"
    });
    assert.equal(result.status, 78, result.stderr);
    assert.equal(result.stdout, "");
    assert.match(result.stderr, /legacy_watchdog_disabled/);
  }
});

test("retired installer cannot create cron jobs or a system watchdog", async (t) => {
  const source = await readFile(path.join(root, "scripts/install-morning-schedule.sh"), "utf8");
  assert.match(source, /legacy_schedule_install_disabled/);
  assert.match(source, /user astock-xhs-source.timer/);
  assert.doesNotMatch(source, /crontab|systemctl|sudo|install -m|OnCalendar=/);
  const temporary = await mkdtemp(path.join(os.tmpdir(), "astock-retired-installer-"));
  t.after(() => rm(temporary, { recursive: true, force: true }));
  const bin = path.join(temporary, "bin");
  await mkdir(bin);
  const marker = path.join(temporary, "called");
  for (const command of ["crontab", "systemctl", "sudo", "install"]) {
    await writeFile(path.join(bin, command), '#!/bin/sh\nprintf called > "$INSTALLER_TEST_MARKER"\nexit 99\n', { mode: 0o700 });
  }
  const result = spawnSync("bash", [path.join(root, "scripts/install-morning-schedule.sh")], {
    env: { PATH: `${bin}:${process.env.PATH ?? ""}`, INSTALLER_TEST_MARKER: marker },
    encoding: "utf8"
  });
  if (result.error?.code === "ENOENT") { t.skip("bash is unavailable"); return; }
  assert.equal(result.status, 78, result.stderr);
  assert.match(result.stderr, /legacy_schedule_install_disabled/);
  await assert.rejects(readFile(marker), /ENOENT/);
});

test("retired repair cannot update code, collect data or perform catch-up", async (t) => {
  const source = await readFile(path.join(root, "scripts/repair-morning-schedule.sh"), "utf8");
  assert.match(source, /legacy_schedule_repair_disabled/);
  assert.match(source, /user astock-xhs-source.timer/);
  assert.doesNotMatch(source, /git (?:pull|fetch)|npm |crontab|systemctl|sudo|exec |--catch-up/);
  const temporary = await mkdtemp(path.join(os.tmpdir(), "astock-retired-repair-"));
  t.after(() => rm(temporary, { recursive: true, force: true }));
  const bin = path.join(temporary, "bin");
  await mkdir(bin);
  const marker = path.join(temporary, "called");
  for (const command of ["git", "npm", "crontab", "systemctl", "sudo", "install", "node"]) {
    await writeFile(path.join(bin, command), '#!/bin/sh\nprintf called > "$INSTALLER_TEST_MARKER"\nexit 99\n', { mode: 0o700 });
  }
  const result = spawnSync("bash", [path.join(root, "scripts/repair-morning-schedule.sh"), "--catch-up"], {
    env: { PATH: `${bin}:${process.env.PATH ?? ""}`, INSTALLER_TEST_MARKER: marker },
    encoding: "utf8"
  });
  if (result.error?.code === "ENOENT") { t.skip("bash is unavailable"); return; }
  assert.equal(result.status, 78, result.stderr);
  assert.match(result.stderr, /legacy_schedule_repair_disabled/);
  await assert.rejects(readFile(marker), /ENOENT/);
});
