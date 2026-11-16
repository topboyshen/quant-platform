# Known limitations (accepted)

Each entry is a limitation we have diagnosed and chosen not to fix now. It is
not a TODO list; if an entry starts blocking a required path, open a new change
with the evidence below.

## KL-001 — Eastmoney names fallback is unreachable from nnG8

| Field | Value |
|---|---|
| Status | **accepted** |
| Since | 2026-11-16 |
| Source | `eastmoney_names` (`https://push2.eastmoney.com/api/qt/clist/get`) |
| Role | fallback A-share code→name source |
| Symptom | `RemoteDisconnected` / `Empty reply from server`, ~40 ms, no HTTP response |
| Impact | fallback only; `tencent_names` primary works; name resolution is not blocked |

### Evidence (2026-11-16)

- nnG8 direct egress `150.158.46.28`: same request → `000` empty reply.
- nnG8 "proxy" path is effectively direct: mihomo has
  `DOMAIN-SUFFIX,eastmoney.com,DIRECT` before `🚪 国内直连`, so qp's
  `via_proxy=true` is nominal for this host.
- Temporary mihomo instance routing all traffic through `🇯🇵日本丨DRTˣ²`:
  still empty reply.
- Same URL/UA/Referer from Mac (home broadband) and Quant-Server
  (`111.229.252.186`): **200** with valid JSON.
- `push2delay` / `push2his`: also reset. `quote.eastmoney.com` and
  `www.eastmoney.com`: 200. TLS handshake succeeds; the reset happens after the
  HTTP request is sent.

Conclusion: Eastmoney resets the push2 API for nnG8's egress IP (data-center /
overseas range), not a qp bug, not a mihomo node failure, and not an Eastmoney
site-wide outage.

### Decision

- Do **not** switch mihomo nodes for this fallback.
- Do **not** add a cross-host dependency just to keep the fallback green.
- Re-evaluate if `tencent_names` coverage degrades or Eastmoney becomes a
  required source. A domestic egress (e.g. an approved reverse proxy on
  Quant-Server) would need a separate approval and evidence.

### Monitoring

`market.sources` records `eastmoney` as failed (currently 7/8). That is expected
and is shown on the panel; it does not raise an `egress:*` alert and does not
fail the market job. If the primary name source changes, open a new limitation
entry instead of silently removing this one.

### How to reproduce

```bash
# from nnG8; same UA/Referer as qp
curl -sS --http1.1 -A 'Mozilla/5.0 ...' -e https://quote.eastmoney.com/ \
  'https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=1&po=1&np=1&fltt=2&invt=2&fid=f12&fs=m%3A1%2Bt%3A2&fields=f12%2Cf13%2Cf14'
# expect: curl: (52) Empty reply from server
```

## Template for future entries

- ID, status, since, source, role, symptom, impact.
- Evidence matrix (at least two independent vantage points).
- Decision and what would trigger a re-evaluation.
- Monitoring signal and reproduction command.
