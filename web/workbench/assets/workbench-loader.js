/* Read-only, same-origin batch reader. Exported strings remain inert JSON data. */
"use strict";

(function (global, factory) {
  const api = factory(global);
  if (typeof module === "object" && module.exports) module.exports = api;
  else global.NNG8WorkbenchLoader = api;
})(typeof window === "undefined" ? globalThis : window, function (global) {
  const PANEL_NAMES = ["today", "progress", "delivery"];
  // projects.json is existing exporter metadata, never an additional read path.
  const FILE_NAMES = new Set(["today.json", "progress.json", "delivery.json", "projects.json"]);
  const MANIFEST_LIMIT = 512 * 1024;
  const PANEL_LIMIT = 2 * 1024 * 1024;
  const TIMEOUT_MS = 12000;
  const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  const record = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

  class ReadFailure extends Error {
    constructor(reason, retry = false) {
      super(reason);
      this.retry = retry;
    }
  }

  function instant(value) {
    if (typeof value !== "string" || value.length > 40
        || !/^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,9})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/.test(value)
        || !Number.isFinite(Date.parse(value))) return false;
    const date = value.slice(0, 10);
    const dateMs = Date.parse(`${date}T00:00:00Z`);
    return Number.isFinite(dateMs) && new Date(dateMs).toISOString().slice(0, 10) === date;
  }

  function parse(bytes) {
    try {
      return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    } catch (_) {
      throw new ReadFailure("JSON 内容无法读取");
    }
  }

  function validateManifest(value) {
    if (!record(value) || value.schemaVersion !== 1 || !instant(value.exportedAt) || !record(value.files)) {
      throw new ReadFailure("清单结构或版本无效");
    }
    for (const [file, entry] of Object.entries(value.files)) {
      if (!FILE_NAMES.has(file) || !record(entry)
          || typeof entry.sha256 !== "string" || !/^[a-f0-9]{64}$/i.test(entry.sha256)
          || !Number.isSafeInteger(entry.bytes) || entry.bytes < 1 || entry.bytes > PANEL_LIMIT) {
        throw new ReadFailure("清单文件记录无效");
      }
    }
    return value;
  }

  function create(dependencies = {}) {
    const fetcher = dependencies.fetch || (global.fetch && global.fetch.bind(global));
    const crypto = dependencies.crypto || global.crypto;
    const schedule = dependencies.setTimeout || global.setTimeout.bind(global);
    const unschedule = dependencies.clearTimeout || global.clearTimeout.bind(global);
    if (typeof fetcher !== "function" || !crypto || !crypto.subtle
        || typeof crypto.subtle.digest !== "function" || typeof global.AbortController !== "function") {
      throw new TypeError("浏览器缺少读取或完整性校验支持");
    }

    async function readBody(response, limit) {
      const length = response.headers && response.headers.get("content-length");
      if (length !== null && length !== undefined && /^\d+$/.test(length) && Number(length) > limit) {
        throw new ReadFailure("来源文件超过大小限制");
      }
      if (response.body && typeof response.body.getReader === "function") {
        const reader = response.body.getReader();
        const chunks = [];
        let count = 0;
        try {
          while (true) {
            const part = await reader.read();
            if (part.done) break;
            if (!(part.value instanceof Uint8Array)) throw new ReadFailure("来源文件无法读取");
            count += part.value.byteLength;
            if (count > limit) {
              // Cancellation is best-effort; the bounded read must never await it.
              Promise.resolve(reader.cancel()).catch(() => {});
              throw new ReadFailure("来源文件超过大小限制");
            }
            chunks.push(part.value);
          }
        } finally {
          reader.releaseLock();
        }
        const joined = new Uint8Array(count);
        let offset = 0;
        for (const chunk of chunks) {
          joined.set(chunk, offset);
          offset += chunk.byteLength;
        }
        return joined.buffer;
      }
      const bytes = await response.arrayBuffer();
      if (!(bytes instanceof ArrayBuffer)) throw new ReadFailure("来源文件无法读取");
      if (bytes.byteLength > limit) throw new ReadFailure("来源文件超过大小限制");
      return bytes;
    }

    async function readBytes(file, limit) {
      // No caller or manifest data can select a URL, origin, method, or body.
      if (file !== "manifest.json" && !PANEL_NAMES.some((name) => file === `${name}.json`)) {
        throw new ReadFailure("来源路径不允许读取");
      }
      const controller = new global.AbortController();
      let timer;
      try {
        const deadline = new Promise((_, reject) => {
          timer = schedule(() => {
            reject(new ReadFailure("来源读取超时（12 秒）"));
            controller.abort();
          }, TIMEOUT_MS);
        });
        const request = (async () => {
          const response = await fetcher(`data/${file}`, {
            method: "GET", cache: "no-store", credentials: "same-origin",
            mode: "same-origin", redirect: "error", signal: controller.signal
          });
          if (!response || response.ok !== true || response.redirected) {
            throw new ReadFailure("来源文件不可用");
          }
          return readBody(response, limit);
        })();
        return await Promise.race([request, deadline]);
      } catch (error) {
        controller.abort();
        throw error instanceof ReadFailure ? error : new ReadFailure("来源文件读取失败");
      } finally {
        unschedule(timer);
      }
    }

    async function readPanel(name, manifest) {
      const file = `${name}.json`;
      if (!own(manifest.files, file)) throw new ReadFailure("导出清单缺少此来源");
      const expected = manifest.files[file];
      const bytes = await readBytes(file, PANEL_LIMIT);
      if (bytes.byteLength !== expected.bytes) throw new ReadFailure("文件字节数与导出清单不符", true);
      let actual;
      try {
        actual = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
          (byte) => byte.toString(16).padStart(2, "0")).join("");
      } catch (_) {
        throw new ReadFailure("来源文件完整性校验失败");
      }
      if (actual !== expected.sha256.toLowerCase()) throw new ReadFailure("文件 SHA-256 与导出清单不符", true);
      const panel = parse(bytes);
      if (!record(panel) || panel.schemaVersion !== 1 || panel.page !== name || !instant(panel.exportedAt)) {
        throw new ReadFailure("来源结构、页面标识或版本无效");
      }
      if (panel.exportedAt !== manifest.exportedAt) throw new ReadFailure("导出批次时间与清单不一致", true);
      return panel;
    }

    async function readBatch(previous) {
      let manifest;
      try {
        manifest = validateManifest(parse(await readBytes("manifest.json", MANIFEST_LIMIT)));
      } catch (error) {
        const reason = error instanceof ReadFailure ? error.message : "清单无法读取";
        throw new Error(`导出清单不可用：${reason}`);
      }
      const panels = { today: null, progress: null, delivery: null, manifest };
      const results = await Promise.all(PANEL_NAMES.map(async (name) => {
        const file = `${name}.json`;
        try {
          const oldManifest = previous && previous.panels.manifest;
          const oldEntry = oldManifest && oldManifest.files[file];
          const entry = manifest.files[file];
          // A retry may keep already verified bytes only when the new manifest
          // binds exactly the same file and export time. Never mix export batches.
          if (previous && previous.panels[name] && oldManifest.exportedAt === manifest.exportedAt
              && oldEntry && entry && oldEntry.bytes === entry.bytes
              && oldEntry.sha256.toLowerCase() === entry.sha256.toLowerCase()) {
            panels[name] = previous.panels[name];
          } else {
            panels[name] = await readPanel(name, manifest);
          }
          return null;
        } catch (error) {
          return { file, reason: error instanceof ReadFailure ? error.message : "来源文件读取失败",
            retry: error instanceof ReadFailure && error.retry };
        }
      }));
      const failures = results.filter(Boolean);
      return { panels, errors: failures.map(({ file, reason }) => ({ file, reason })),
        verifiedCount: PANEL_NAMES.filter((name) => panels[name] !== null).length,
        retry: failures.some((failure) => failure.retry) };
    }

    return {
      async load() {
        let result = await readBatch(null);
        // Exporter writes individual files atomically and publishes manifest last.
        // One reread can resolve that short overlap; this is never a polling loop.
        if (result.retry) result = await readBatch(result);
        return { panels: result.panels, errors: result.errors, verifiedCount: result.verifiedCount };
      }
    };
  }

  return { create };
});
