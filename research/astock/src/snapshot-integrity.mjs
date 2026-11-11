import { createHash } from "node:crypto";

export function canonicalJson(value) {
  const normalize = (item) => {
    if (item === null || typeof item === "string" || typeof item === "boolean") return item;
    if (typeof item === "number" && Number.isFinite(item)) return item;
    if (Array.isArray(item)) return item.map(normalize);
    if (item && Object.getPrototypeOf(item) === Object.prototype) {
      return Object.fromEntries(Object.keys(item).sort().map((key) => [key, normalize(item[key])]));
    }
    throw new Error("snapshot contains a non-JSON value");
  };
  return JSON.stringify(normalize(value));
}

export function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

export function jsonHash(value) {
  return sha256(canonicalJson(value));
}

export function validDate(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

export function instantMs(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

// Microsecond-exact twin of instantMs. Date.parse keeps only milliseconds, but the
// Python bridge records and compares observation instants at microsecond
// precision (datetime.fromisoformat truncates beyond 6 digits, so do we).
// Returns BigInt microseconds since the epoch, or null for anything instantMs rejects.
export function instantMicros(value) {
  if (instantMs(value) === null) return null;
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!match) return null;
  const wholeSecondMs = Date.parse(`${match[1]}${match[3]}`);
  if (!Number.isFinite(wholeSecondMs)) return null;
  return BigInt(wholeSecondMs) * 1000n + BigInt((match[2] ?? "").slice(0, 6).padEnd(6, "0"));
}

// Same, for a cutoff supplied as an ISO string (with offset) or a valid Date.
// Anything else (empty/invalid string, null/undefined, number, boolean, object) is null:
// a missing cutoff must never compare equal to another missing instant.
export function cutoffMicros(value) {
  if (typeof value === "string") return instantMicros(value);
  if (value instanceof Date) {
    const ms = value.getTime();
    return Number.isFinite(ms) ? BigInt(ms) * 1000n : null;
  }
  return null;
}

// ISO-8601 (UTC) for a microsecond instant, with a 6-digit fraction only when needed.
export function isoFromMicros(micros) {
  const ms = micros / 1000n;
  const sub = micros - ms * 1000n;
  const base = new Date(Number(ms)).toISOString();
  return sub === 0n ? base : base.replace(/Z$/, `${String(sub).padStart(3, "0")}Z`);
}
