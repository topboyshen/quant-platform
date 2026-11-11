import { readFileSync } from "node:fs";

const calendar = JSON.parse(readFileSync(new URL("../config/us-equity-calendar.json", import.meta.url), "utf8"));
export const US_EQUITY_CALENDAR_ID = "us_equity_regular_v1";
const SYMBOLS = new Set([".DJI", ".INX", ".IXIC"]);
if (calendar.schemaVersion !== "us-equity-calendar.v1" ||
    calendar.calendarId !== US_EQUITY_CALENDAR_ID || calendar.timezone !== "America/New_York") {
  throw new Error("US equity calendar contract invalid");
}
const eastern = new Intl.DateTimeFormat("en-CA", {
  timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"
});

function isoDay(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    throw new Error("US equity session date invalid");
  }
  const day = new Date(`${value}T00:00:00Z`);
  if (!Number.isFinite(day.getTime()) || day.toISOString().slice(0, 10) !== value) {
    throw new Error("US equity session date invalid");
  }
  if (!calendar.supportedYears.includes(day.getUTCFullYear())) {
    throw new Error(`US equity calendar year unsupported: ${day.getUTCFullYear()}`);
  }
  return day;
}

export function usEquityCloseTime(day) {
  const parsed = isoDay(day);
  if ([0, 6].includes(parsed.getUTCDay()) || calendar.closedDates.includes(day)) return null;
  return calendar.earlyCloses[day] ?? calendar.regularClose;
}

function easternParts(value) {
  const instant = new Date(value);
  if (!Number.isFinite(instant.getTime())) throw new Error("US equity cutoff invalid");
  const parts = Object.fromEntries(eastern.formatToParts(instant).map(({ type, value }) => [type, value]));
  return { instant, day: `${parts.year}-${parts.month}-${parts.day}`, time: `${parts.hour}:${parts.minute}:${parts.second}` };
}

export function latestCompletedUsSession(cutoff) {
  const { day: today, time } = easternParts(cutoff);
  let day = today;
  for (let attempt = 0; attempt < 15; attempt += 1) {
    const close = usEquityCloseTime(day);
    if (close !== null && (day < today || time >= `${close}:00`)) return day;
    const previous = isoDay(day);
    previous.setUTCDate(previous.getUTCDate() - 1);
    day = previous.toISOString().slice(0, 10);
  }
  throw new Error("US equity calendar has no completed session within search window");
}

export function classifyUsIndexSession(source, cutoff) {
  if (source?.marketCalendar !== US_EQUITY_CALENDAR_ID || !SYMBOLS.has(source?.marketSymbol ?? source?.symbol)) {
    throw new Error("US equity evidence calendar or symbol unsupported");
  }
  const close = usEquityCloseTime(source.sessionDate);
  const evidence = easternParts(source.dataAsOf);
  if (close === null || evidence.day !== source.sessionDate || evidence.time !== `${close}:00` ||
      evidence.instant.getUTCMilliseconds() !== 0) {
    throw new Error("US equity evidence session timestamp invalid");
  }
  if (evidence.instant.getTime() > new Date(cutoff).getTime()) throw new Error("US equity evidence after cutoff");
  const expectedSessionDate = latestCompletedUsSession(cutoff);
  if (source.sessionDate > expectedSessionDate) throw new Error("US equity evidence newer than completed session");
  return Object.freeze({
    tracked: true, kind: "market", stale: source.sessionDate !== expectedSessionDate,
    ageHours: (new Date(cutoff).getTime() - evidence.instant.getTime()) / 3_600_000,
    policy: "last_completed_us_session", sessionDate: source.sessionDate, expectedSessionDate
  });
}
