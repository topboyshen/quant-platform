import { lstat, readFile } from "node:fs/promises";
import path from "node:path";
import { isResultPresentation } from "./report-presentation.mjs";

const MAX_POLICY_BYTES = 16 * 1024;

function validReportView(value) {
  return value === "full" || isResultPresentation(value);
}

// Store only the non-sensitive presentation choice in the immutable run input.
// The notification policy itself (including approval metadata) is never archived.
export async function loadFrozenNotificationPresentation(projectRoot) {
  const file = path.join(projectRoot, "config", "notifications.json");
  const info = await lstat(file);
  if (!info.isFile() || info.isSymbolicLink() || info.size < 1 || info.size > MAX_POLICY_BYTES) {
    throw new Error("config/notifications.json 无效");
  }
  const policy = JSON.parse(await readFile(file, "utf8"));
  if (!policy || typeof policy !== "object" || Array.isArray(policy)
      || policy.schemaVersion !== "1.0" || typeof policy.deliveryEnabled !== "boolean"
      || policy.route !== "telegram" || policy.payloadMode !== "formal_report_markdown_document") {
    throw new Error("config/notifications.json 无效");
  }
  const reportView = Object.hasOwn(policy, "reportView") ? policy.reportView : "full";
  if (!validReportView(reportView)) throw new Error("config/notifications.json reportView 无效");
  return { reportView };
}

// Older sealed inputs did not capture this choice. Unknown is preferable to
// reconstructing history from today's mutable notification policy.
export function reportViewFromFrozenConfiguration(configuration) {
  if (!configuration || !Object.hasOwn(configuration, "notificationPresentation")) return null;
  const frozen = configuration.notificationPresentation;
  if (!frozen || typeof frozen !== "object" || Array.isArray(frozen)
      || Object.keys(frozen).length !== 1 || !validReportView(frozen.reportView)) {
    throw new Error("invalid frozen notification presentation");
  }
  return frozen.reportView;
}
