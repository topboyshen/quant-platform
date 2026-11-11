import { lstat, mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { jsonHash, sha256 } from "./snapshot-integrity.mjs";
import { verifiedFinancialDocument } from "./document-evidence.mjs";

export function documentManifest(context, cutoffInstant) {
  const entries = [];
  for (const stock of context.data?.watchlist ?? []) {
    const doc = stock.officialDisclosure?.financialSummary;
    if (doc?.status !== "available") continue;
    // Validate even an after-cutoff document: it remains collected evidence,
    // although the renderer must exclude it from cutoff facts.
    verifiedFinancialDocument(stock, context.sources ?? [], cutoffInstant);
    entries.push({ code: stock.code, sha256: doc.pdfSha256, bytes: doc.pdfBytes });
  }
  if (entries.length > 3 || new Set(entries.map((e) => e.code)).size !== entries.length) {
    throw new Error("invalid official document population");
  }
  return entries.toSorted((a, b) => a.code.localeCompare(b.code));
}

async function realDirectory(directory, create = false) {
  if (create) await mkdir(directory, { mode: 0o700 });
  const stat = await lstat(directory);
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error("official PDF directory must not be a symlink");
}

async function pdfBytes(file, expected) {
  const stat = await lstat(file);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size !== expected.bytes || stat.size > 4 * 1024 * 1024) {
    throw new Error("invalid official PDF artifact");
  }
  const body = await readFile(file);
  if (body.subarray(0, 5).toString("ascii") !== "%PDF-" || sha256(body) !== expected.sha256) {
    throw new Error("official PDF byte hash mismatch");
  }
  return body;
}

export async function copyDocumentAssets({ projectRoot, documentStore, archiveDirectory, manifest }) {
  if (!manifest.length) return;
  let store = documentStore;
  if (!store) {
    store = projectRoot;
    for (const part of ["data", "inbox", "akshare-cache", "cninfo-pdf"]) {
      store = path.join(store, part);
      await realDirectory(store);
    }
  } else await realDirectory(store);
  const destination = path.join(archiveDirectory, "evidence");
  await realDirectory(destination, true);
  const copied = new Set();
  for (const entry of manifest) {
    if (copied.has(entry.sha256)) continue;
    const name = `${entry.sha256}.pdf`;
    const body = await pdfBytes(path.join(store, name), entry);
    await writeFile(path.join(destination, name), body, { flag: "wx", mode: 0o600 });
    copied.add(entry.sha256);
  }
}

export async function verifyDocumentAssets(archiveDirectory, input) {
  const expected = documentManifest(input.compilerContext, input.cutoffInstant);
  if (jsonHash(expected) !== jsonHash(input.sourceDocuments ?? [])) throw new Error("official PDF manifest mismatch");
  if (!expected.length) return;
  const directory = path.join(archiveDirectory, "evidence");
  await realDirectory(directory);
  for (const entry of expected) await pdfBytes(path.join(directory, `${entry.sha256}.pdf`), entry);
}
