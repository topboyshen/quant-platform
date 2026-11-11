import { cutoffMicros, instantMicros, isoFromMicros, validDate } from "./snapshot-integrity.mjs";

// A date label is not a publication timestamp. For date-only records use the
// earlier of the actual observation and the END of the declared date as the
// conservative availability bound. Never render that bound as publishedAt.
export function disclosureEvidenceInstant(record) {
  if (record?.timePrecision !== "date") return record?.publishedAt ?? record?.dataAsOf ?? null;
  if (!validDate(record.disclosureDate)) return null;
  const observed = instantMicros(record.observedAt);
  if (observed === null) return null;
  const dayStart = BigInt(Date.parse(`${record.disclosureDate}T00:00:00+08:00`)) * 1000n;
  if (observed < dayStart || record.publishedAt || record.dataAsOf !== record.observedAt) return null;
  const dayEnd = dayStart + 86_400_000_000n;
  return isoFromMicros(observed < dayEnd ? observed : dayEnd);
}

export function disclosureQueryState(item, sources, cutoffInstant, { previousDate } = {}) {
  // An empty/invalid cutoff is rejected up front: coverage can only be judged against a real instant,
  // and two missing instants (null === null) must never count as "equal".
  const cutoff = cutoffMicros(cutoffInstant);
  if (cutoff === null) return { state: "unverified", queriedAt: null, reason: "invalid_cutoff" };
  const query = item?.officialDisclosure;
  if (query?.status !== "available") return { state: query?.status === "partial" ? "partial" : "unavailable", queriedAt: null };
  const ids = Array.isArray(query.sourceIds) ? query.sourceIds : [];
  const references = ids.map((id) => sources.find((source) => source?.id === id));
  if (!ids.length || references.some((source) => !source || source.sourceType !== "official_disclosure_query")) {
    return { state: "unverified", queriedAt: null };
  }
  const time = instantMicros(query.queriedAt);
  if (time === null || references.some((source) => instantMicros(source.dataAsOf) !== time)) {
    return { state: "unverified", queriedAt: null };
  }
  if (time > cutoff) return { state: "after_cutoff", queriedAt: query.queriedAt };
  const coverage = query.queryCoverage;
  if (coverage?.kind === "window") {
    const start = instantMicros(coverage.windowStartAt);
    const end = instantMicros(coverage.cutoffAt);
    const observed = instantMicros(coverage.observedThroughAt);
    const requiredStart = validDate(previousDate) ? BigInt(Date.parse(`${previousDate}T00:00:00+08:00`)) * 1000n : null;
    const counts = [coverage.pagesFetched, coverage.total, coverage.returned, coverage.pageSize, coverage.maxPages];
    const valid = coverage.complete === true && coverage.reason === "all_query_pages_collected" &&
      counts.every(Number.isInteger) && coverage.pagesFetched >= 1 && coverage.pagesFetched <= coverage.maxPages &&
      coverage.maxPages >= 1 && coverage.maxPages <= 8 && coverage.pageSize >= 1 && coverage.pageSize <= 100 &&
      coverage.maxPages * coverage.pageSize <= 120 &&
      coverage.total === coverage.returned && coverage.returned >= 0 && coverage.returned <= coverage.pagesFetched * coverage.pageSize &&
      start !== null && end !== null && end === cutoff && instantMicros(coverage.queriedAt) === time &&
      observed !== null && start <= observed && observed <= time &&
      requiredStart !== null && start <= requiredStart;
    if (!valid) return { state: "partial", queriedAt: query.queriedAt, coverage: "incomplete", reason: coverage.reason };
    return { state: "available", queriedAt: query.queriedAt, coverage: "declared_complete",
      observedThroughAt: coverage.observedThroughAt, atomicSnapshot: false };
  }
  // Legacy/unknown pagination flags are not a typed, cutoff-bound proof.
  return { state: "available", queriedAt: query.queriedAt, coverage: "limited" };
}

export function filingIsEligible(item, sources, { cutoffInstant, previousDate, background = false, code } = {}) {
  if (item?.sourceType !== "official_announcement" || (code && String(item.code) !== String(code))) return false;
  if (!validDate(item.disclosureDate) || !validDate(previousDate)) return false;
  if (background ? item.disclosureDate >= previousDate : item.disclosureDate < previousDate) return false;
  const cutoff = cutoffMicros(cutoffInstant);
  if (cutoff === null) return false;
  const cutoffDate = new Date(Number(cutoff / 1000n) + 8 * 3_600_000).toISOString().slice(0, 10);
  if (item.disclosureDate > cutoffDate) return false;
  const evidence = instantMicros(disclosureEvidenceInstant(item));
  if (evidence === null || evidence > cutoff) return false;
  const ids = item.sourceIds;
  return Array.isArray(ids) && ids.length > 0 && ids.every((id) => {
    const source = sources.find((value) => value?.id === id);
    if (!source || source.sourceType !== "official_announcement") return false;
    const time = instantMicros(disclosureEvidenceInstant(source));
    return time !== null && time === evidence && time <= cutoff && source.title === item.title &&
      (source.timePrecision !== "date" || source.disclosureDate === item.disclosureDate);
  });
}
