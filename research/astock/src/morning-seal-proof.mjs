// One proof contract for the sealed delivery command and the final outbox
// boundary. This pure validator never reads files, changes state or sends.
export function validateMorningSealProof({ seal, proofKind, targetDate,
  reportFile, reportSha256, lastRunSha256, lastRun }) {
  const fail = () => { throw new Error("sealed_report_proof_invalid"); };
  const hash = (value) => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
  const research = proofKind === "research";
  if (!research && proofKind !== "legacy") fail();
  if (!seal || typeof seal !== "object" || Array.isArray(seal)
      || seal.schemaVersion !== (research ? "astock.research-seal.v1" : "astock.xhs-draft.v1")
      || seal.kind !== (research ? "research_seal" : "xhs_draft")
      || seal.state !== (research ? "SEALED" : "DRAFT_ONLY")
      || seal.targetDate !== targetDate) fail();
  if (!research && (seal.publishEligible !== false || seal.humanApprovalRequired !== true)) fail();
  const source = seal.source;
  if (!source || typeof source !== "object" || Array.isArray(source)
      || source.reportFile !== reportFile || source.reportSha256 !== reportSha256
      || source.lastRunFile !== "work/last-run.json" || source.lastRunSha256 !== lastRunSha256
      || source.publicationGate !== "passed" || source.evidenceReview !== "passed") fail();
  if (research && (typeof source.frozenCutoffInstant !== "string"
      || !Number.isFinite(Date.parse(source.frozenCutoffInstant))
      || source.frozenCutoffInstant !== lastRun?.frozenCutoffInstant
      || !hash(source.contextSha256) || source.contextSha256 !== lastRun?.contextSha256)) fail();
  return { proofKind: research ? "research_seal" : "xhs_draft",
    sealGeneratedAt: research ? seal.generatedAt ?? null : null,
    contextSha256: research ? source.contextSha256 : null };
}
