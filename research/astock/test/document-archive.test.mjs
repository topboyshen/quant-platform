import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { documentFixture } from "./helpers/document-fixture.mjs";
import { documentManifest, copyDocumentAssets, verifyDocumentAssets } from "../src/document-archive.mjs";

async function setup(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), "astock-pdf-archive-test-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const fixture = documentFixture();
  const store = path.join(root, "store");
  const archive = path.join(root, "archive");
  await mkdir(store); await mkdir(archive);
  const name = `${fixture.doc.pdfSha256}.pdf`;
  await writeFile(path.join(store, name), fixture.body, { mode: 0o600 });
  const cutoffInstant = "2026-09-14T00:00:00Z";
  const manifest = documentManifest(fixture.context, cutoffInstant);
  const input = { compilerContext: fixture.context, cutoffInstant, sourceDocuments: manifest };
  return { ...fixture, root, store, archive, name, manifest, input };
}

test("archived PDF bytes survive removal of the rolling source cache", async (t) => {
  const f = await setup(t);
  await copyDocumentAssets({ documentStore: f.store, archiveDirectory: f.archive, manifest: f.manifest });
  await rm(f.store, { recursive: true });
  await verifyDocumentAssets(f.archive, f.input);
  assert.deepEqual(await readFile(path.join(f.archive, "evidence", f.name)), f.body);
});

test("missing or altered source bytes cannot be archived as verified evidence", async (t) => {
  const f = await setup(t);
  await writeFile(path.join(f.store, f.name), Buffer.alloc(f.body.length));
  await assert.rejects(copyDocumentAssets({ documentStore: f.store, archiveDirectory: f.archive, manifest: f.manifest }), /hash mismatch/);
});

test("replay refuses a missing, altered or symlink-replaced PDF", async (t) => {
  const f = await setup(t);
  await copyDocumentAssets({ documentStore: f.store, archiveDirectory: f.archive, manifest: f.manifest });
  const file = path.join(f.archive, "evidence", f.name);
  await writeFile(file, Buffer.alloc(f.body.length));
  await assert.rejects(verifyDocumentAssets(f.archive, f.input), /hash mismatch/);
  await rm(file);
  await assert.rejects(verifyDocumentAssets(f.archive, f.input), /ENOENT/);
  await symlink(path.join(f.store, f.name), file);
  await assert.rejects(verifyDocumentAssets(f.archive, f.input), /invalid official PDF artifact/);
});

test("an evidence-bearing packet cannot omit or change its archived manifest", async (t) => {
  const f = await setup(t);
  await assert.rejects(verifyDocumentAssets(f.archive, { ...f.input, sourceDocuments: [] }), /manifest mismatch/);
  const changed = structuredClone(f.input); changed.sourceDocuments[0].sha256 = "0".repeat(64);
  await assert.rejects(verifyDocumentAssets(f.archive, changed), /manifest mismatch/);
});

test("a symlinked document store is rejected before copying", async (t) => {
  const f = await setup(t);
  const link = path.join(f.root, "linked-store");
  await symlink(f.store, link);
  await assert.rejects(copyDocumentAssets({ documentStore: link, archiveDirectory: f.archive, manifest: f.manifest }), /must not be a symlink/);
});
