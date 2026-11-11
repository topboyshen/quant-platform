// Notification filesystem ownership: paths, bounded reads, immutable publication and one shared queue lock.
import path from "node:path";
import { randomBytes } from "node:crypto";
import { createRequire } from "node:module";
import { closeSync, lstatSync, openSync } from "node:fs";
import { access, chmod, link, lstat, mkdir, open, readFile, realpath, rename, stat, unlink } from "node:fs/promises";
import { validateDeliveryPolicy, validateDeliveryMode, PROJECT_ROOT, MAX_LAST_RUN_BYTES, MAX_POLICY_BYTES, EVENT_STATES } from "./notification-contract.mjs";

function layout(projectRoot = PROJECT_ROOT) {
  const root = path.join(projectRoot, "work", "notifications");
  return {
    root,
    payloads: path.join(root, "payloads"),
    receipts: path.join(root, "receipts"),
    // 普通发送的平台回执单独放一层，避免与 correction gate receipt 混用同一文件名。
    sendReceipts: path.join(root, "receipts", "send"),
    pending: path.join(root, "pending"),
    sending: path.join(root, "sending"),
    sent: path.join(root, "sent"),
    ambiguous: path.join(root, "ambiguous"),
    dead: path.join(root, "dead"),
    authorizations: path.join(root, "authorizations"),
    lock: path.join(root, ".drain.lock")
  };
}



async function ensureLayout(projectRoot = PROJECT_ROOT) {
  const paths = layout(projectRoot);
  await mkdir(paths.root, { recursive: true, mode: 0o700 });
  await chmod(paths.root, 0o700);
  for (const key of ["payloads", "receipts", "sendReceipts", "authorizations", ...EVENT_STATES]) {
    await mkdir(paths[key], { recursive: true, mode: 0o700 });
    await chmod(paths[key], 0o700);
  }
  return paths;
}



async function fileExists(filePath) {
  try {
    await access(filePath);
    return true;
  } catch {
    return false;
  }
}



async function readBounded(filePath, maxBytes) {
  const info = await lstat(filePath);
  if (!info.isFile() || info.isSymbolicLink()) throw new Error("不是普通文件");
  if (info.size > maxBytes) throw new Error("文件超过大小限制");
  return readFile(filePath);
}



async function readLastRun(projectRoot) {
  const filePath = path.join(projectRoot, "work", "last-run.json");
  try {
    const raw = await readBounded(filePath, MAX_LAST_RUN_BYTES);
    const parsed = JSON.parse(raw.toString("utf8"));
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}



async function loadDeliveryPolicy(projectRoot) {
  const policyPath = path.join(projectRoot, "config", "notifications.json");
  const raw = await readBounded(policyPath, MAX_POLICY_BYTES);
  return validateDeliveryPolicy(JSON.parse(raw.toString("utf8")));
}



async function deliveryIsAuthorized(projectRoot, deliveryMode) {
  const mode = validateDeliveryMode(deliveryMode);
  const policy = await loadDeliveryPolicy(projectRoot);
  return mode === "telegram" && policy.deliveryEnabled === true;
}



async function publishOnce(finalPath, data) {
  const temporary = path.join(
    path.dirname(finalPath),
    `.${path.basename(finalPath)}.${process.pid}.${randomBytes(6).toString("hex")}.tmp`
  );
  const handle = await open(temporary, "wx", 0o600);
  try {
    await handle.writeFile(data);
    await handle.sync();
  } finally {
    await handle.close();
  }
  try {
    await link(temporary, finalPath);
    await syncDirectory(path.dirname(finalPath));
    return true;
  } catch (error) {
    if (error.code === "EEXIST") return false;
    throw error;
  } finally {
    await unlink(temporary).catch(() => {});
  }
}



async function syncDirectory(directory) {
  const handle = await open(directory, "r");
  try {
    await handle.sync();
  } catch (error) {
    // Some local test filesystems do not support directory fsync. Actual I/O
    // failures must remain failures, especially after a message was sent.
    if (!["EINVAL", "ENOTSUP", "EISDIR", "EBADF"].includes(error.code)) throw error;
  } finally {
    await handle.close();
  }
}



async function isRegularEventFile(filePath) {
  try {
    const info = await lstat(filePath);
    return info.isFile() && !info.isSymbolicLink();
  } catch {
    return false;
  }
}

async function eventAlreadyExists(paths, eventId) {
  for (const state of EVENT_STATES) {
    if (await isRegularEventFile(path.join(paths[state], `${eventId}.json`))) return state;
  }
  return null;
}



async function eventStates(paths, eventId) {
  const states = [];
  for (const state of EVENT_STATES) {
    if (await isRegularEventFile(path.join(paths[state], `${eventId}.json`))) states.push(state);
  }
  return states;
}



async function readFixedFile(projectRoot, relativeFile, relativeRoot, maxBytes) {
  const filePath = path.join(projectRoot, ...relativeFile.split("/"));
  const rootPath = path.join(projectRoot, ...relativeRoot.split("/"));
  const info = await lstat(filePath);
  if (!info.isFile() || info.isSymbolicLink()) throw new Error("固定产物不是普通文件");
  if (info.size > maxBytes) throw new Error("固定产物超过大小限制");
  const body = await readFile(filePath);
  const canonicalRoot = await realpath(rootPath);
  const canonicalFile = await realpath(filePath);
  const relative = path.relative(canonicalRoot, canonicalFile);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error("固定产物越出允许目录");
  }
  return { body, path: canonicalFile };
}



async function writeAtomicJson(filePath, value) {
  const directory = path.dirname(filePath);
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const temporary = path.join(directory, `.${path.basename(filePath)}.${randomBytes(6).toString("hex")}`);
  const handle = await open(temporary, "wx", 0o600);
  try {
    await handle.writeFile(JSON.stringify(value, null, 2) + "\n");
    await handle.sync();
  } finally {
    await handle.close();
  }
  try {
    await rename(temporary, filePath);
  } catch (error) {
    // 落盘失败不留半成品。
    await unlink(temporary).catch(() => {});
    throw error;
  }
  // 目录 fsync 让“文件已落盘”这件事更稳；部分文件系统（如 macOS 的 APFS）不支持，
  // 失败时忽略即可——文件本身已经 sync 过，且 rename 是原子替换。
  const directoryHandle = await open(directory, "r");
  try {
    await directoryHandle.sync();
  } catch {
    // 目录 fsync 不受支持：不因此判定回执落盘失败。
  } finally {
    await directoryHandle.close();
  }
}



async function moveEvent(fromPath, destinationDirectory) {
  const destination = path.join(destinationDirectory, path.basename(fromPath));
  await rename(fromPath, destination);
  await syncDirectory(destinationDirectory);
  await syncDirectory(path.dirname(fromPath));
  return destination;
}



const require = createRequire(import.meta.url);
let DatabaseSyncClass;

function isSqliteExperimentalWarning(warning, type) {
  const message = warning instanceof Error ? warning.message : String(warning ?? "");
  const warningType = typeof type === "string" ? type : type?.type || warning?.name;
  return warningType === "ExperimentalWarning" && message.includes("SQLite is an experimental feature");
}

function loadDatabaseSync() {
  if (DatabaseSyncClass) return DatabaseSyncClass;
  const previous = process.emitWarning;
  const suppressor = function sqliteWarningFilter(warning, type, code, ...rest) {
    if (isSqliteExperimentalWarning(warning, type)) return undefined;
    return previous.call(process, warning, type, code, ...rest);
  };
  process.emitWarning = suppressor;
  try {
    const imported = require("node:sqlite");
    if (typeof imported.DatabaseSync !== "function") {
      const error = new TypeError("node:sqlite DatabaseSync export missing");
      error.code = "SQLITE_IMPORT_INVALID";
      throw error;
    }
    DatabaseSyncClass = imported.DatabaseSync;
    return DatabaseSyncClass;
  } finally {
    if (process.emitWarning === suppressor) process.emitWarning = previous;
  }
}

function lockInspection(lockPath) {
  try {
    return { info: lstatSync(lockPath) };
  } catch (error) {
    if (error?.code === "ENOENT") return { missing: true };
    return { refuse: true };
  }
}

function existingLockMustFailClosed(info) {
  // A pre-existing empty file, directory, or symlink is not a database and
  // must not be opened: DatabaseSync would initialize a 0-byte file.
  return !info || info.isSymbolicLink() || !info.isFile() || info.size === 0;
}

function publishDurableDatabase(DatabaseSync, lockPath) {
  // Commit the schema before the caller holds the lock. A kill before this
  // commit can leave a 0-byte file, which later acquires must not initialize.
  // After this commit the file is a real database and crash recovery can open it.
  let db;
  try {
    db = new DatabaseSync(lockPath);
    db.exec("PRAGMA busy_timeout = 0");
    db.exec("BEGIN EXCLUSIVE");
    db.exec("CREATE TABLE IF NOT EXISTS holder (token TEXT NOT NULL)");
    db.exec("COMMIT");
    db.close();
    return true;
  } catch {
    try { db?.exec("ROLLBACK"); } catch { /* unusable */ }
    try { db?.close(); } catch { /* fail closed */ }
    return false;
  }
}

function holdExclusive(DatabaseSync, lockPath) {
  let db;
  let acquired = false;
  for (let attempt = 0; attempt < 8 && !acquired; attempt += 1) {
    try {
      db = new DatabaseSync(lockPath);
      db.exec("PRAGMA busy_timeout = 0");
      db.exec("BEGIN EXCLUSIVE");
      acquired = true;
    } catch (error) {
      try { db?.close(); } catch { /* busy or corrupt */ }
      db = undefined;
      if (!String(error?.message || "").includes("database is locked")) return null;
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 2);
    }
  }
  if (!acquired || !db) return null;
  try {
    db.exec("CREATE TABLE IF NOT EXISTS holder (token TEXT NOT NULL)");
    const token = randomBytes(16).toString("hex");
    db.prepare("DELETE FROM holder").run();
    db.prepare("INSERT INTO holder (token) VALUES (?)").run(token);
  } catch {
    try { db.exec("ROLLBACK"); } catch { /* unusable connection */ }
    try { db.close(); } catch { /* fail closed */ }
    return null;
  }
  let released = false;
  return async () => {
    if (released) return;
    released = true;
    try {
      db.exec("COMMIT");
    } catch {
      try { db.exec("ROLLBACK"); } catch { /* connection already ending */ }
    } finally {
      try { db.close(); } catch { /* only this holder */ }
    }
  };
}

async function acquireDrainLock(paths) {
  let DatabaseSync;
  try {
    DatabaseSync = loadDatabaseSync();
  } catch {
    return null;
  }
  // The lock path is never unlinked or replaced. A pre-existing 0-byte file,
  // directory, symlink, plaintext, or corrupt database fails closed. Only a
  // missing path may be created, and only with O_EXCL so two creators cannot
  // both initialize the same inode. The loser of that race does not open a
  // still-empty file.
  const first = lockInspection(paths.lock);
  if (first.refuse || (first.info && existingLockMustFailClosed(first.info))) return null;
  let createdByUs = false;
  if (first.missing) {
    try {
      const fd = openSync(paths.lock, "wx", 0o600);
      closeSync(fd);
      createdByUs = true;
    } catch (error) {
      if (error?.code !== "EEXIST") return null;
    }
  }
  if (!createdByUs) {
    const again = lockInspection(paths.lock);
    if (!again.info || existingLockMustFailClosed(again.info)) return null;
  } else {
    const created = lockInspection(paths.lock);
    if (!created.info || created.info.isSymbolicLink() || !created.info.isFile()) return null;
    if (!publishDurableDatabase(DatabaseSync, paths.lock)) return null;
  }
  return holdExclusive(DatabaseSync, paths.lock);
}

export { layout, ensureLayout, fileExists, readBounded, readLastRun, loadDeliveryPolicy, deliveryIsAuthorized, publishOnce, eventAlreadyExists, eventStates, readFixedFile, writeAtomicJson, moveEvent, acquireDrainLock };
