"""PROPOSED /opt/nng8-sync/export_entry.py; root-owned, not installed by this task.

Only supported production invocation:
    /usr/bin/python3 -I /opt/nng8-sync/export_entry.py
"""
import sys

if not sys.flags.isolated or sys.argv[1:]:
    raise SystemExit(1)
sys.path[:0] = ["/opt/nng8-sync", "/opt/nng8-sync/candidate/integration"]
from sync.exporter import main

raise SystemExit(main())
