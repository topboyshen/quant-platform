#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path


def main() -> int:
    hashes_to_sizes: dict[str, set[int]] = {}
    for manifest_path in sorted(Path('/var/lib/research-ledger/manifests/astock').glob('astock-*.json')):
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        for item in manifest.get('artifacts', []):
            if item.get('kind') == 'notification' and isinstance(item.get('artifact_hash'), str):
                hashes_to_sizes.setdefault(item['artifact_hash'], set()).add(int(item.get('bytes', -1)))
    matches = [path for path in (Path('/var/lib/research-ledger/cas/sha256')).rglob('*') if path.is_file() and path.name in hashes_to_sizes]
    zero = 0
    nonzero = 0
    size_consistent = 0
    for path in matches:
        size = path.stat().st_size
        if size == 0:
            zero += 1
        else:
            nonzero += 1
        if size in hashes_to_sizes[path.name]:
            size_consistent += 1
    print(json.dumps({'raw_match_objects': len(matches), 'zero_byte_raw_matches': zero, 'nonzero_raw_matches': nonzero, 'metadata_size_consistent_matches': size_consistent}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
