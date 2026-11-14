"""M7.6 frontend package: router coverage, shared state usage, runbook."""
from __future__ import annotations

from pathlib import Path

APP = Path(__file__).resolve().parents[2]
FRONTEND = APP / "frontend"
DEPLOY = APP / "deploy"


def test_router_covers_all_nav_sections():
    text = (FRONTEND / "src" / "router.ts").read_text(encoding="utf-8")
    for label in ("总览", "行情", "研究", "信号", "报告", "运维", "设置"):
        assert f"label: '{label}'" in text, label
    assert "Placeholder" not in text
    for route in ("research", "signals", "reports", "settings", "market", "ops"):
        assert f"path: '{route}'" in text, route


def test_all_data_pages_use_shared_async_state():
    views = list((FRONTEND / "src" / "views").rglob("*.vue"))
    users = [p for p in views if "AsyncState" in p.read_text(encoding="utf-8")]
    assert len(users) >= 12, [p.name for p in views if "AsyncState" in p.read_text(encoding="utf-8")]


def test_build_script_typechecks():
    package = (FRONTEND / "package.json").read_text(encoding="utf-8")
    assert "vue-tsc --noEmit" in package and "vite build" in package


def test_runbook_documents_build_release_and_rollback():
    text = (DEPLOY / "M7-FRONTEND-RUNBOOK.md").read_text(encoding="utf-8")
    assert "npm ci" in text and "npm run build" in text
    assert "/opt/qp/current/frontend" in text
    assert "逐页验收" in text and "回滚" in text
    assert "node_modules" in text and "sourcemap" in text
    assert "/next/settings" in text

def test_layout_collapses_navigation_on_narrow_screens():
    text = (FRONTEND / "src" / "views" / "Layout.vue").read_text(encoding="utf-8")
    assert "menu-toggle" in text and "@media (max-width: 860px)" in text
    assert "n-drawer" in text.lower() and "navOpen" in text

def test_mobile_styles_keep_tables_scrollable():
    css = (FRONTEND / "src" / "styles.css").read_text(encoding="utf-8")
    assert "@media (max-width: 860px)" in css
    assert "n-data-table-base-table-body" in css and "overflow-x: auto" in css
    assert "n-data-table-table" in css and "min-width" in css
    assert "white-space: nowrap" in css
    main = (FRONTEND / "src" / "main.ts").read_text(encoding="utf-8")
    assert "styles.css" in main
