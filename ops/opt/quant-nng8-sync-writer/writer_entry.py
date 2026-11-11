"""PROPOSED /opt/nng8-sync/writer_entry.py; root-owned, not installed by this task."""
import sys

if not sys.flags.isolated or sys.argv[1:]:
    raise SystemExit(1)
sys.path[:0] = ["/opt/quant-nng8-sync-writer", "/opt/quant-nng8-sync-writer/candidate/integration"]
from sync.writer import main

raise SystemExit(main())
