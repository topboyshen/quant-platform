# nnG8 nginx (quant-platform P2, 2026-11-11)

- `nng8-dashboard-public` → `/etc/nginx/sites-available/nng8-dashboard-public` (80 ACME/redirect + 443 single entry)
- `quant-platform-qd-headers.conf` → `/etc/nginx/snippets/`
- `quantdinger-8443.template` → `/etc/nginx/sites-available/quantdinger-8443`; replace `__APPROVED_SIGNALS_SECRET_PATH__` with the real path kept only on the host (never commit it).
- `nng8-dashboard` (127.0.0.1:8080 workbench origin), `quantdinger-proxy.conf` (snippet) – unchanged, for reference.

Deploy: copy, `sudo nginx -t`, `sudo systemctl reload nginx`. Rollback: restore the pre-change tar, `nginx -t`, reload.
