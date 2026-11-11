#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/trading-radar"
cd "$ROOT"

warn() { printf 'WARN: %s\n' "$*" >&2; }
ok() { printf 'OK: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

user_name="${SUDO_USER:-${USER:-$(id -un)}}"
if id -nG "$user_name" | tr ' ' '\n' | grep -qx docker; then
  ok "user $user_name is in docker group"
else
  warn "user $user_name is not in docker group"
  warn "run: sudo usermod -aG docker $user_name ; then re-login via SSH"
fi

if docker ps >/dev/null 2>&1; then
  ok "docker daemon is accessible without sudo"
else
  fail "docker daemon is not accessible. Fix docker group membership before migration."
fi

docker compose -f "$ROOT/docker-compose.yml" config >/tmp/radar-compose-preflight.yml
ok "docker compose config is valid"

for dir in data logs models rl_models; do
  if [[ -d "$ROOT/$dir" ]]; then
    ok "$dir/ exists"
  else
    fail "$dir/ is missing"
  fi
done

mihomo_config="${MIHOMO_CONFIG:-/home/ubuntu/mihomo/config.yaml}"
if [[ -f "$mihomo_config" ]]; then
  ok "mihomo config exists: $mihomo_config"
else
  fail "mihomo config is missing: $mihomo_config (set MIHOMO_CONFIG=/path/to/config.yaml if needed)"
fi

if grep -Eq '^\s*(port|mixed-port):\s*7890\b' "$mihomo_config"; then
  ok "mihomo HTTP/mixed proxy listens on 7890 in config"
else
  warn "mihomo config does not explicitly expose port/mixed-port 7890; monitor RADAR_*_PROXY uses http://mihomo:7890"
fi

if grep -Eq '^\s*external-controller:\s*["'\'' ]*0\.0\.0\.0:9090["'\'' ]*$' "$mihomo_config"; then
  ok "mihomo external-controller is reachable inside compose network"
else
  warn "mihomo external-controller is not 0.0.0.0:9090; host mapping ${MIHOMO_CONTROLLER_PORT:-9092}->9090 may be unusable"
fi

printf '\nPort occupancy before migration:\n'
python3 - <<'PY'
import socket
for port in [6379, 7890, 7891, 9092, 9091, 3000, 8080, 8090, 8091, 8081]:
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
        print(f"{port}: LISTEN (expected before cutover if old services are running)")
    except Exception:
        print(f"{port}: free")
    finally:
        s.close()
PY

printf '\nCurrent listeners:\n'
ss -ltnp '( sport = :6379 or sport = :7890 or sport = :7891 or sport = :9092 or sport = :9091 or sport = :3000 or sport = :8080 or sport = :8090 or sport = :8091 or sport = :8081 )' || true

printf '\nPreflight complete. If app ports are occupied, stop old services gracefully before docker compose up.\n'
