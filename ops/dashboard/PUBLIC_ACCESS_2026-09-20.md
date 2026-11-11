# nnG8 IP HTTPS deployment — completed 2026-09-20

## Final result

- Public URL: https://150.158.46.28/ (standard HTTPS, no explicit port).
- Cloud TCP 443 rule was already present when the confirmation turn began; verified instance and rule, did not duplicate it. Existing cloud TCP 80 retained.
- Added host IPv4 UFW rules only for TCP 80/443. Existing SSH and other rules preserved. 8080 remains loopback-only.
- Formal Let's Encrypt IP certificate issued successfully using isolated Certbot 5.8.0 and shortlived profile.
- Certificate serial: 05FFAB5EF98DEBD37B205AD8D7021147BE0C.
- Validity: 2026-09-19 21:26:45 UTC through 2026-09-26 13:26:44 UTC.
- SHA-256 fingerprint: 24:58:7F:44:D8:F7:88:B3:60:B3:E3:F7:D4:34:74:C9:F2:6F:83:DA:19:12:36:09:5D:CE:3A:B1:C2:57:66:C4. Public and on-disk certificate matched.
- IP SAN: 150.158.46.28. External curl verified trusted chain/IP normally, without insecure flags.
- Public verification: 47/47 passed (public-verification.json): anonymous GET/HEAD and wrong password rejected; authenticated pages/data/assets succeed with no-store; encoded-path counterexamples rejected; HTTP redirects; dotfiles denied; ACME probe succeeds.
- Public authenticated HTML, JS, today.json and projects.json hashes matched production files at comparison time.
- TLS1.2 external handshake passed; TLS1.3 tested on VPS because management-machine curl lacks it. TLS1.0/1.1 rejected by server with protocol-version alert.
- Dedicated root-owned executable deploy hook installed at /etc/letsencrypt/renewal-hooks/deploy/nng8-dashboard-nginx (0755). Password hash root:www-data 0640.
- Sole nng8-dashboard-cert-renew.timer enabled/active; checks at 03:00 and 15:00 CST with up to 10-minute jitter. Next observed: 2026-09-20 15:07:10 CST.
- Renewal dry-run with --run-deploy-hooks succeeded. The tool labeled Nginx's normal stderr success diagnostics as 'error output'; they state syntax OK/test successful, and the overall command returned 0.
- Natural renewal has NOT occurred yet; only issuance and renewal rehearsal are accepted.
- Renewal-hook Nginx reload confirmed in journal at 2026-09-20 06:30:46 CST; post-rehearsal external HTTPS still returned authenticated-gate 401 with trusted certificate.
- Chrome showed the normal HTTPS Basic login dialog. In-app preview could not display the authentication challenge. Password is delivered in a private local file, not in this record or Git.
- Existing business code, timers, FROZEN/DRAFT_ONLY state and raw-data permissions unchanged. No backups/replays/backfill.

### Deployed configuration hashes

| Artifact | SHA-256 |
|---|---|
| nginx-public.conf | 04e65b5bc56e11506056ef75938d0b1e3e21e39d443578b12d54e0e81901eb9f |
| renew-nginx.sh | 3426b360008c0f87c517d84305da659a02f2c1545ca844b55f82b164f9256a88 |
| renew service | 12dfd176a246a2993ea300f72d0222b7e71aa46f4a641f40ab57cfba18e892ef |
| renew timer | fb15355b108314b64c658f31d8263e0d29d32f7722debd9093f47982090a92ba |
| public verification JSON | b07154a8ee0f6e39720f52901274e3236422feb2c76919384478ba72cbe70d2a |

### Rollback scope (not executed)

Disable only the new public site and validate/reload Nginx, disable the dedicated certificate timer, and remove only the two new host firewall rules and dashboard cloud 443 rule if authorized. Preserve localhost:8080, SSH, business units and all report data. Do not uninstall shared Nginx or remove other cloud rules.

---

## Historical preparation notes (superseded by final result above)

Target: https://150.158.46.28 (user selected direct IP).
User approved Let's Encrypt subscriber terms and automatic renewal.

## Current state

- Existing localhost dashboard on 127.0.0.1:8080 remains HTTP 200.
- Cloud instance is Shanghai lhins-8zzutrhq, Ubuntu-nnG8, verified in console.
- Cloud TCP 80 already allowed. TCP 443 rule prepared in console, NOT submitted.
- Explicit action-time approval requested for cloud TCP 443 from all IPv4 and host 80/443; awaiting answer.
- Host UFW still permits only SSH; no firewall change yet.
- Bootstrap site installed: /etc/nginx/sites-available/nng8-dashboard-public and enabled symlink.
- Bootstrap listener on 80 serves only ACME challenge files; all other requests return 404. It does NOT proxy dashboard data.
- Nginx configuration test and reload passed. Private site unaffected.
- Password hash installed root:www-data 0640 at /etc/nginx/nng8-dashboard.htpasswd. No cleartext password in server configuration or this directory.
- Final HTTPS configuration, renewal units, and deploy hook are prepared, NOT activated.
- Isolated Certbot 5.8.0 installation started at /opt/nng8-acme; package downloads still in progress at last check. No system or business package upgrade.
- No certificate requested/issued yet, no public HTTPS listener yet, no renewal timer activated yet.

## Remaining sequence

1. Receive action-time firewall confirmation; submit only TCP 443 cloud rule and verify exact instance/result.
2. Host allow TCP 80/443, preserving other rules. Verify ACME path from outside.
3. Confirm isolated Certbot installation/version, use webroot IP shortlived staging dry-run, then issue trusted certificate named nng8-dashboard-ip.
4. Install executable root-owned renew-nginx.sh in /etc/letsencrypt/renewal-hooks/deploy/; activate final site after certificate exists, nginx -t then reload.
5. Validate HTTPS certificate/IP SAN from outside; unauthenticated page, assets and every data path return 401. Validate authenticated pages/data against localhost.
6. Install sole twice-daily renewal timer and dry-run --run-deploy-hooks. Confirm actual hook reload and next run.
7. Deliver public URL and private local login file to user; never print password, use URL credentials, or add credentials to Git.

Preserve FROZEN, DRAFT_ONLY, business timers, SSH, localhost dashboard and data permissions. No backups, replays, backfill or Git changes.
