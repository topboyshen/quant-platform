// Pure settlement for one sender call. A thrown call is unproven even if a
// boolean would have said "not started"; only a returned attestation is proof.
function settleSenderCall({ entered, thrown, result }) {
  if (!entered) return { disposition: "not_entered", reason: "not_entered" };
  if (thrown) return { disposition: "ambiguous", reason: "sender_threw_unproven" };
  if (result?.ok === true && result.started === true && result.ack && result.timedOut !== true) {
    return { disposition: "acked", reason: null };
  }
  if (result && result.started === false && result.timedOut !== true && result.ok !== true) {
    return { disposition: "pending", reason: result.receiptRejectReason || "proven_not_started" };
  }
  return {
    disposition: "ambiguous",
    reason: result?.receiptRejectReason || (result?.timedOut === true ? "timed_out" : "unproven_after_invoke")
  };
}

export { settleSenderCall };
