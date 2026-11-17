"""Containers intentionally retired in M9.

They stay in the read-only inventory for audit, but a stopped retired container is an
expected state: it must not page and it must not be counted as an unhealthy container.
"""

from __future__ import annotations

RETIRED_CONTAINERS = frozenset(
    {
        "radar-mihomo",
        "radar-prometheus",
        "radar-grafana",
        "radar-alertmanager",
        "radar-node-exporter",
        "radar-quantdinger-backend",
        "radar-quantdinger-frontend",
        "radar-integration-api",
        "radar-macro-risk",
        "radar-runtime-observer",
    }
)
