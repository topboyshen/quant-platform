# CNINFO seal → relay dry-run chain regression

Regression for the 2026-09-17 silent seal miss: report sources use
`披露日期` + `实际采集时间`, xhs-draft must seal, research_relay prepare must
pass, and dry-run must keep `would_send=false` / `sent=0` (no HTTP).

## Run

```sh
cd /home/ubuntu/signal-bus
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest tests.test_cninfo_seal_relay_chain -v
```

Also included in:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -p 'test_*.py' -v
```

Fixture builder (Node, offline): `scripts/build_cninfo_seal_fixture.mjs`
