# Sealed research relay

This replaces the normal `build_research_digest.py` + `post_digest.py` service
entry, not the upstream research workflow. Historical scripts are left intact.

The production CLI has fixed roots and no date, destination, recipient, or
backfill override:

```sh
python3 -B /home/ubuntu/signal-bus/scripts/research_relay.py --dry-run --json
python3 -B /home/ubuntu/signal-bus/scripts/research_relay.py --json
```

Dry-run validates today's sealed formal report, even outside the send window.
It does not read `config/pending.env`, notification configuration or prior
delivery state, create directories/locks, write files, invoke models or connect
to the network. Output is only fixed reason codes and non-sensitive metadata;
`would_send` is always false. It is not a backfill authorization.

## Dependency and date contract

1. AStock completes the current Shanghai-day morning occurrence and run.
2. Existing XHS draft generation produces the immutable manifest sealing the
   formal report and last-run bytes. The relay reads only the manifest proof,
   never `content.body`, and cannot publish the draft.
3. A preserved/additive `OnSuccess` on the draft service starts the relay. Retry
   timers at 08:05/08:10 cover a transient explicitly-unsent or rejected result.
4. Signal Gate's merged digest runs at 08:15; its consumer independently requires
   today's structured research contract, not the old 72-hour acceptance rule.

Relay sends only Monday–Friday 08:00:00 through 08:13:59 Shanghai time, with a
completed run no older than two hours. Weekend/late/manual invocations cannot
POST. The upstream sealed report already passed trading-calendar validation;
the relay does not guess holidays or silently carry forward an old report.
The 08:14:00 cutoff leaves more than the 20-second HTTP timeout before the 08:15
merged digest. A weekday classified by the upstream calendar as non-trading is
not relayed; this version does not extend the downstream contract to communicate
calendar skips, so that downstream alert distinction remains an explicit limit.

All occurrence/run time ordering, successful child exit, fixed report paths,
publication/evidence gates, empty blockers, live/partial data, frozen cutoff,
human semantic-review label and three source hashes are validated. Symlink
parents/files, duplicate-key/invalid JSON, oversize files, changed artifacts and
missing seal proofs hold the relay. The mutable live context is deliberately
not read; its hash comes from the sealed run and matching manifest source.

## Summary contract

`prepare_research(project_root, now)` is a read-only Python API returning the
payload/proof in memory. `build_payload(project_root, now)` returns the payload.
These test seams are not CLI path/date overrides.

The market `as_of` comes from section 2 and must precede the report day. Section
6 supplies the first complete `[推断] 摘要` block (otherwise the first inference
block), including every support/counter-evidence continuation. All section 7
risks, cited source entries, partial-data limits and human-review limits remain.
If no structured inference exists, a labelled basic report carries original
facts; no analysis or counter-evidence is invented. Selection of one of several
blocks is disclosed. Overlong retained content fails instead of truncating away
risks or counter-evidence. Query/fragment credentials and unsafe source links are
removed with an explicit limitation note. No order or approval signal is emitted.

For a basic report's first nonempty headline line, a single unordered Markdown
bullet (`-`, `+`, or `*`, followed by spaces/tabs) is removed only when immediately
followed by an existing `[事实]` or `[推断]` label. Plain labelled headlines and
later lines keep the existing whitespace normalization only; unlabelled
continuations do not acquire labels.
Missing labels, unsupported wrappers and missing/invalid references still hold.

Conclusions retain `[事实]`/`[推断]` labels and require source references. If the
original headline has no inline reference, its retained support references are
copied explicitly into the conclusion, never invented. Inference blocks without
a cited support field or counter-evidence hold. Each continuation remains in its
original cited evidence item. The only uncited counter-evidence allowed is the
exact pre-existing risk-block placeholder `未提供结构化反证来源，不升级为事实结论。`;
it is preserved as a disclosed limitation, never inserted to make a report pass.
Compound references such as `[来源 2、3]` or `[来源 2 3]` resolve every
member; malformed or unknown references hold. Contract limits are 1,200 characters
per string, 24 items per list and 20,000 summary characters; exceeding a limit
holds rather than dropping counter-evidence or risk text.

The payload is `source=nng8`, `signal_type=research_summary`, with a version 1
`research_contract` and `research_summary` keys `conclusion`, `evidence`,
`counter_evidence`, `risks`, `sources`, `limitations`. `text/markdown` are backwards
compatible safe previews, not the primary consumer contract.

## Delivery safety

Normal mode reads only the existing POST keys `DEDIROCK_DIGEST_URL`,
`DEDIROCK_PENDING_SECRET`, `X_SOURCE`, `TLS_VERIFY`. HTTPS certificate checking is
mandatory; HTTP/redirects, userinfo and unverified TLS are not accepted.
Default `TLS_VERIFY=1` uses normal certificate-chain and hostname verification.
Process proxy environment variables are ignored in both modes.

For the existing IP endpoint whose self-signed certificate lacks a SAN, the
independent relay may use an explicitly provisioned certificate-identity pin.
The old `TLS_VERIFY=0` value alone **never** permits a connection: it additionally
requires both fixed files, installed through an already authenticated SSH path:

- `config/dedirock-relay-ca.crt`: one public PEM certificate, no private key.
- `config/relay-tls-pin.json`: exactly `version: 1`, `host` matching the existing
  endpoint hostname, and `certificate_sha256` matching that certificate's DER.

This explicit mode uses a dedicated CA with `CERT_REQUIRED`, keeping chain and
certificate-time validation. Since the existing certificate has no SAN,
hostname checking is replaced by **both** exact endpoint-host checking and the
exact peer-certificate DER SHA256. The custom connection performs these checks
after the TLS handshake and before any HTTP request bytes can be written.
Missing/malformed trust files, local/peer pin mismatch, chain failures, host
changes, CONNECT proxies and redirects fail closed. The relay does not create or
refresh trust pins automatically; certificate rotation needs an independently
authenticated trust-file update. No Nginx, private-key or pending-env changes are
made by this program. Dry-run never reads either trust file.

HMAC signs `timestamp + '.' + exact UTF-8 JSON bytes`. Acknowledgement must be a
2xx JSON object with `status=accepted`, `source=nng8`, `day=as_of`; its private
`path` or any response body is never logged or stored.

Per-day lock and atomic state live at `out/relay-delivery/YYYY-MM-DD.{lock,json}`.
State contains dates, hashes and fixed status/reason only. `sending` is fsynced
before POST; confirmed same-hash delivery is skipped. A changed same-day hash,
crash in `sending`, timeout or other ambiguous result requires manual review,
never an automatic resend. Explicit request rejection or proven non-connection
may retry at the next scheduled opportunity. No external result is guessed.

## Offline regression tests

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -p 'test_*.py' -v
```

Tests use temporary artifact trees, mock transport and a socket guard. They
cover sealed inputs, old/current report dates, mutation and symlink resistance,
gates/hashes/cutoff, basic-mode disclosure, retained counter-evidence/references,
source credential cleanup, dry-run isolation, window recheck, HMAC/receipts,
confirmed deduplication and ambiguous/rejected delivery recovery boundaries.
An optional integration test imports a sibling Signal Gate consumer when present
and validates six fixture payload shapes (including a basic list headline)
entirely in memory. A standalone
production relay checkout skips that one test; all other tests are independent.

## CNINFO seal chain regression (2026-09-17)

Offline full-chain check that a report with CNINFO `披露日期` + `实际采集时间`
source lines can build an xhs-draft seal and pass `prepare_research` / dry-run
relay validation without HTTP send:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest tests.test_cninfo_seal_relay_chain -v
```

See `CNINFO_SEAL_RELAY_CHAIN.md`. Seal-failure Telegram alerts:
`scripts/morning_seal_alert.sh` (wired via `astock-xhs-draft` OnFailure and
`run_research_relay_with_alert.sh`). Verify with
`ALERT_DRY_RUN=1 scripts/morning_seal_alert.sh --dry-run-notify`.
