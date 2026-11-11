"""Smoke tests for public entry points and compatibility import paths."""

from __future__ import annotations

import ast
import importlib
import importlib.util
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT_COMPAT_MODULES = (
    "backtest_engine.py",
    "backtest_pro.py",
    "data_schema.py",
    "ml_predictor.py",
    "ml_training.py",
    "mx_quota.py",
    "rl_agent.py",
    "rl_environment.py",
    "rl_features.py",
    "rl_strategy.py",
)
_ALLOWED_SYS_PATH_INSERT = {
    "dashboard/app.py",
    "legacy/radar_monitor.py",
    "legacy/radar_monitor_v2.py",
    "scripts/auto_trigger_updater.py",
    "scripts/doctor.py",
    "tests/conftest.py",
    "tools/ak_query.py",
    "tools/akshare_daily.py",
    "tools/daily_data_collector.py",
    "scripts/check_swing_eod_retry.py",
    "scripts/export_host_textfile_metrics.py",
    "scripts/research_digest.py",
    "scripts/update_index_close_cache.py",
    "scripts/verify_p0_radar.py",
}
_ALLOWED_DUPLICATE_STEMS = {
    "cache": {"radar/cache.py", "radar/ml_serve/cache.py", "radar_pro/cache.py"},
    "radarctl": {"radarctl.py", "tools/radarctl.py"},
}
_LARGE_FILE_BUDGETS = {
    "dashboard/app.py": 760,
    "radar_pro.py": 760,
    "radar/backtest/backtest_pro.py": 750,
    "radar/data/market_data_v2.py": 820,  # host branch additions
}
_MONITOR_ENGINE_MIXINS = {
    "HealthServerMixin",
    "EventModeMixin",
    "PollModeMixin",
    "AlertDispatchMixin",
    "CleanupMixin",
    "StrategyMixin",
}
_ALLOWED_PRINT_FILES = {
    "benchmark_tracker.py",
    "dashboard/akshare_data.py",
    "dashboard/app.py",
    "monitor.py",
    "mx_client/client.py",
    "mx_client/helpers.py",
    "mx_client/parsers.py",
    "radar/schemas/data_schema.py",
    "radar_monitor_v3.py",
    "radar_swing_eod.py",
    "radar_pro.py",
    "radar_trainer.py",
    "radarctl.py",
    "run_backtest_comparison.py",
    "run_benchmark.py",
    "scripts/auto_trigger_updater.py",
    "scripts/check_quant_monday_acceptance.py",
    "scripts/check_swing_eod_retry.py",
    "scripts/doctor.py",
    "scripts/export_execution_digest.py",
    "scripts/export_host_textfile_metrics.py",
    "scripts/index_close_backend_probe.py",
    "scripts/report_git_worktree_hygiene.py",
    "scripts/research_digest.py",
    "scripts/sync_hermes_workbench_allowlist.py",
    "scripts/update_index_close_cache.py",
    "scripts/validate_vix_csi300.py",
    "scripts/verify_p0_eligibility.py",
    "scripts/verify_p0_radar.py",
    "scripts/verify_production_pin.py",
    "deploy/approved-signals/app.py",
    "deploy/approved-signals/tls_proxy.py",
    "deploy/etf-diagnostics-20260916/deploy.py",
    "deploy/etf-diagnostics-20260916/smoke.py",
    "tests/fixtures/host_textfile_datasource/exporter_pre20260908.py",
}
_OPTIONAL_LOW_COVERAGE_MODULES = {
    "radar/ml_serve/grpc_client.py",
    "radar/ml_serve/grpc_serve.py",
    "radar/strategy/rl_agent.py",
    "radar/strategy/rl_environment.py",
    "radar/strategy/rl_features.py",
    "radar/strategy/rl_strategy.py",
}
_IGNORED_RUNTIME_PREFIXES = (
    ".codex-deploy/",
    ".trash/",
    ".venv/",
    "QuantDinger/",  # Separate subtree with its own backend guardrails.
    "backups/",
    "deploy/personal-",
    "deployments/",
    "releases/",
    "runtime/",
    "venv/",
)


def _parse_project_file(relative_path: str) -> ast.Module:
    return ast.parse((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))


def _is_ignored_runtime_path(relative_path: str) -> bool:
    return relative_path.startswith(_IGNORED_RUNTIME_PREFIXES)


def _has_dataclass_slots(class_name: str, relative_path: str) -> bool:
    tree = _parse_project_file(relative_path)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and getattr(decorator.func, "id", "") == "dataclass":
                return any(
                    keyword.arg == "slots" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
                    for keyword in decorator.keywords
                )
        return False
    return False


def test_radar_pro_package_imports_without_heavy_ml_deps():
    radar_pro = importlib.import_module("radar_pro")
    radar_pro._ENTRY = None
    market_fetcher = importlib.import_module("radar_pro.market_fetcher")
    cache = importlib.import_module("radar_pro.cache")

    assert radar_pro._ENTRY is None
    assert hasattr(market_fetcher, "MarketDataFetcher")
    assert hasattr(cache, "CacheManager")


def test_canonical_domain_module_paths_exist():
    ml_package = importlib.import_module("radar.ml")
    data_schema = importlib.import_module("radar.schemas.data_schema")
    backtest = importlib.import_module("radar.backtest.engine")

    assert ml_package.__all__ == ["FeatureEngineer", "MLPredictor"]
    assert hasattr(data_schema, "ColumnNames")
    assert hasattr(backtest, "BacktestEngine")


def test_split_radar_pro_entrypoints_are_importable():
    import radar_pro

    radar_pro._ENTRY = None
    for module_name in (
        "radar_pro_cli",
        "radar_pro_train",
        "radar_pro_backtest",
    ):
        module = importlib.import_module(module_name)
        assert callable(module.main)
        assert radar_pro._ENTRY is None


def test_split_radar_pro_entrypoints_expose_help_without_loading_config(capsys):
    import radar_pro

    radar_pro._ENTRY = None
    assert radar_pro._ENTRY is None
    entries = (
        radar_pro.entry_monitor,
        radar_pro.entry_train,
        radar_pro.entry_backtest,
    )
    assert radar_pro._ENTRY is not None
    for entry in entries:
        with pytest.raises(SystemExit) as exc_info:
            entry(["--help"])
        assert exc_info.value.code == 0

    assert "Radar Pro" in capsys.readouterr().out


def test_legacy_radar_py_forwards_to_monitor_entry():
    module_path = PROJECT_ROOT / "radar.py"
    spec = importlib.util.spec_from_file_location("legacy_radar_entry", module_path)
    assert spec is not None and spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.monitor_main is not None
    assert callable(module.main)


def test_root_compat_modules_do_not_use_star_imports():
    for relative_path in ROOT_COMPAT_MODULES:
        tree = _parse_project_file(relative_path)
        star_imports = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
        ]
        assert not star_imports, f"{relative_path} should proxy symbols without import *"


def test_sys_path_insert_is_limited_to_script_bootstraps():
    offenders = []
    for path in PROJECT_ROOT.rglob("*.py"):
        relative_path = path.relative_to(PROJECT_ROOT).as_posix()
        if _is_ignored_runtime_path(relative_path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_sys_path_insert = (
                isinstance(func, ast.Attribute)
                and func.attr == "insert"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "path"
                and isinstance(func.value.value, ast.Name)
                and func.value.value.id == "sys"
            )
            if is_sys_path_insert and relative_path not in _ALLOWED_SYS_PATH_INSERT:
                offenders.append(relative_path)
    assert offenders == []


def test_hot_event_dataclasses_use_slots():
    expected = {
        "radar/core/push_adapter.py": ("MarketDataEvent", "AlertEvent"),
        "radar/alert/batcher.py": ("AlertItem",),
        "radar/alert/alert_manager.py": ("AlertItem",),
        "radar/alert/cooldown_v2.py": ("_CooldownEntry",),
        "dashboard/alert_system.py": ("AlertEvent",),
    }
    missing = [
        f"{relative_path}:{class_name}"
        for relative_path, class_names in expected.items()
        for class_name in class_names
        if not _has_dataclass_slots(class_name, relative_path)
    ]
    assert missing == []


def test_event_bus_exposes_typed_context_manager():
    event_bus = importlib.import_module("radar.core.event_bus")

    assert hasattr(event_bus, "EventBusProtocol")
    assert hasattr(event_bus, "event_bus_context")
    with event_bus.event_bus_context(backend="memory") as bus:
        assert hasattr(bus, "emit")
        assert hasattr(bus, "shutdown")


def test_monitor_engine_mixins_are_flat_and_bounded():
    for relative_path in ("radar/core/monitor_engine.py", "radar/core/monitor_engine_v2.py"):
        tree = _parse_project_file(relative_path)
        engine = next(
            node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "MonitorEngine"
        )
        base_names = {getattr(base, "id", "") for base in engine.bases}
        assert base_names == _MONITOR_ENGINE_MIXINS
        assert len(engine.bases) == len(base_names)

        module = importlib.import_module(relative_path[:-3].replace("/", "."))
        mro_names = [cls.__name__ for cls in module.MonitorEngine.__mro__]
        assert len(mro_names) == len(set(mro_names))


def test_monitor_engine_v3_uses_service_container_injection():
    services_module = importlib.import_module("radar.core.services")
    engine_module = importlib.import_module("radar.core.monitor_engine_v3")

    assert hasattr(services_module, "MonitorEngineServices")
    init_annotations = engine_module.MonitorEngineV3.__init__.__annotations__
    assert init_annotations.get("services") == "Optional[MonitorEngineServices]"


def test_duplicate_module_stems_are_explicitly_allowlisted():
    stems: dict[str, set[str]] = {}
    for path in PROJECT_ROOT.rglob("*.py"):
        relative_path = path.relative_to(PROJECT_ROOT).as_posix()
        if _is_ignored_runtime_path(relative_path):
            continue
        stems.setdefault(path.stem, set()).add(relative_path)

    duplicates = {stem: paths for stem, paths in stems.items() if len(paths) > 1}
    tracked_duplicates = {stem: paths for stem, paths in duplicates.items() if stem in {"cache", "radarctl"}}
    assert tracked_duplicates == _ALLOWED_DUPLICATE_STEMS


def test_large_files_do_not_grow_past_current_split_budget():
    offenders = []
    for relative_path, max_lines in _LARGE_FILE_BUDGETS.items():
        line_count = len((PROJECT_ROOT / relative_path).read_text(encoding="utf-8").splitlines())
        if line_count > max_lines:
            offenders.append(f"{relative_path}:{line_count}>{max_lines}")
    assert offenders == []


def test_print_usage_is_limited_to_cli_or_main_demo_files():
    offenders = []
    for path in PROJECT_ROOT.rglob("*.py"):
        relative_path = path.relative_to(PROJECT_ROOT).as_posix()
        if _is_ignored_runtime_path(relative_path) or relative_path.startswith(
            ("legacy/", "tools/")
        ):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        has_print = any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print"
            for node in ast.walk(tree)
        )
        if has_print and relative_path not in _ALLOWED_PRINT_FILES:
            offenders.append(relative_path)
    assert offenders == []


def test_data_schema_demo_prints_are_main_guarded():
    tree = _parse_project_file("radar/schemas/data_schema.py")
    module_prints = [
        node
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "print"
    ]
    assert module_prints == []
    main_blocks = [
        node
        for node in tree.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and ast.unparse(node.test) == "__name__ == '__main__'"
    ]
    assert main_blocks


def test_optional_rl_and_grpc_modules_are_excluded_from_core_coverage_budget():
    import tomllib

    data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    omitted = set(data["tool"]["coverage"]["run"]["omit"])
    assert "radar/strategy/rl_*.py" in omitted
    assert "radar/ml_serve/grpc_*.py" in omitted
    assert _OPTIONAL_LOW_COVERAGE_MODULES


def test_docker_compose_keeps_runtime_assets_as_project_mounts():
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "dockerfile: Dockerfile.monitor-slim" in compose
    assert "dockerfile: Dockerfile.ml-serve" in compose
    assert "dockerfile: Dockerfile.dashboard" in compose
    assert "./data:/app/data" in compose
    assert "./logs:/app/logs" in compose
    assert "./models:/app/models:ro" in compose
    assert "monitor-data:/app/data" not in compose
    assert "network_mode: host" not in compose
    assert "docker.1ms.run/library/redis:7-alpine" in compose
    assert (
        "docker.1ms.run/metacubex/mihomo@sha256:"
        "9211361a3ed85305383288d38c05e85862d6d5a590c10007c37fc059cca82e9c"
        in compose
    )
    assert "metacubex/mihomo:latest" not in compose
    assert "${MIHOMO_CONFIG:-/home/ubuntu/mihomo/config.yaml}" in compose
    assert "RADAR_ALL_PROXY=http://mihomo:7890" in compose
    assert "RADAR_HTTP_PROXY=http://mihomo:7890" in compose
    assert "RADAR_HTTPS_PROXY=http://mihomo:7890" in compose
    assert "127.0.0.1:${MIHOMO_CONTROLLER_PORT:-9092}:9090" in compose
    assert "docker.1ms.run/prom/prometheus:v2.53.0" in compose
    assert "docker.1ms.run/grafana/grafana:11.0.0" in compose
    assert "redis://redis:6379/0" in compose
    assert "http://ml-serve:8090" in compose
    assert "radar-net:" in compose


def test_docker_prometheus_scrapes_active_compose_service_names():
    prometheus = (PROJECT_ROOT / "deploy/prometheus/prometheus.yml").read_text(encoding="utf-8")

    assert "macro-risk-producer:8093" in prometheus
    assert "monitor:8080" not in prometheus
    assert "ml-serve:8090" not in prometheus
    assert "dashboard:8081" not in prometheus
    assert "172.18.0.1" not in prometheus


def test_docker_build_context_excludes_runtime_assets():
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert "data" in dockerignore
    assert "logs" in dockerignore
    assert "models" in dockerignore
    assert "rl_models" in dockerignore
    assert "*.db" in dockerignore


def test_compose_migration_runbook_defines_permissions_and_rollback():
    runbook = (PROJECT_ROOT / "deploy" / "COMPOSE_MIGRATION_RUNBOOK.md").read_text(encoding="utf-8")

    assert "sudo usermod -aG docker" in runbook
    assert "MIHOMO_CONFIG" in runbook
    assert "docker compose exec -T mihomo /mihomo -t -d" in runbook
    assert "重新登录" in runbook or "重新 SSH" in runbook
    assert "不要用 `kill -9`" in runbook
    assert "一分钟回滚" in runbook
    assert "scripts/compose_rollback.sh" in runbook


def test_compose_rollback_script_uses_graceful_stop_not_kill9():
    rollback = (PROJECT_ROOT / "scripts" / "compose_rollback.sh").read_text(encoding="utf-8")
    cutover = (PROJECT_ROOT / "scripts" / "compose_cutover.sh").read_text(encoding="utf-8")
    preflight = (PROJECT_ROOT / "scripts" / "compose_migration_preflight.sh").read_text(encoding="utf-8")

    assert "down --remove-orphans" in rollback
    assert "sudo docker" in rollback
    assert "unit_exists" in rollback
    assert "No installed systemd unit found" in rollback
    assert '"${compose_cmd[@]}" up -d --build' in cutover
    assert "sudo docker for cutover" in cutover
    assert "ERROR: docker compose up failed" in cutover
    assert "ERROR: health check failed" in cutover
    assert "cleanup_container_name_conflicts" in cutover
    assert '"${docker_cmd[@]}" rm -f' in cutover
    assert "host data directories are preserved" in cutover
    assert "prepare_observability_data_dirs" in cutover
    assert "compose_core_is_running" in cutover
    assert "skipping legacy shutdown" in cutover
    assert "65534:65534" in cutover
    assert "472:472" in cutover
    assert "9091/-/healthy" in cutover
    assert "3000/api/health" in cutover
    assert "radar-ml-serve" in cutover
    assert "8090|8091" in cutover
    assert "kill -TERM" in cutover
    assert "compose_rollback.sh" in cutover
    assert "kill -9" not in rollback
    assert "kill -9" not in cutover
    assert "SIGKILL" not in rollback
    assert "SIGKILL" not in cutover
    assert "docker daemon is accessible" in preflight
    assert "docker group" in preflight
    assert "mihomo config exists" in preflight
    assert "MIHOMO_CONFIG" in preflight
    assert "9092" in preflight
