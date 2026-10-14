#!/usr/bin/env bash
# Prometheus + Grafana, run natively. No Docker, no Linux VM.
#
# Docker Desktop costs ~2GB of RAM for a VM whose only job is to host two Go
# binaries that run perfectly well on macOS. On a 16GB machine that 2GB is the
# difference between the model fitting in memory and paging to swap -- which
# measured as a 3.4x throughput swing, i.e. a 3.4x swing in cost per token.
# Native Grafana + Prometheus cost about 200MB between them.
#
#   ./scripts/monitoring.sh start | stop | status
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN="$ROOT/data/monitoring"; mkdir -p "$RUN/prom-data" "$RUN/grafana-data" "$RUN/logs"

# 3001 because a local dev server commonly owns 3000.
GRAFANA_PORT=3001
PROM_PORT=9090

start_prometheus() {
  if curl -sf -m 2 "http://127.0.0.1:$PROM_PORT/-/healthy" >/dev/null 2>&1; then
    echo "prometheus already up on :$PROM_PORT"; return
  fi
  nohup prometheus \
    --config.file="$ROOT/monitoring/prometheus/prometheus.yml" \
    --storage.tsdb.path="$RUN/prom-data" \
    --storage.tsdb.retention.time=30d \
    --web.listen-address="127.0.0.1:$PROM_PORT" \
    --web.enable-lifecycle \
    > "$RUN/logs/prometheus.log" 2>&1 &
  echo $! > "$RUN/prometheus.pid"
  printf "prometheus starting"
  for _ in $(seq 1 40); do
    curl -sf -m 2 "http://127.0.0.1:$PROM_PORT/-/healthy" >/dev/null 2>&1 && { echo " -> up on :$PROM_PORT"; return; }
    printf "."; sleep 1
  done
  echo " -> TIMED OUT; see $RUN/logs/prometheus.log" >&2; exit 1
}

start_grafana() {
  if curl -sf -m 2 "http://127.0.0.1:$GRAFANA_PORT/api/health" >/dev/null 2>&1; then
    echo "grafana already up on :$GRAFANA_PORT"; return
  fi
  local homepath
  homepath="$(brew --prefix grafana)/share/grafana"

  # Point Grafana's provisioning at the repo so dashboards and datasources stay
  # version-controlled, exactly as they were under Docker.
  TOKENRATE_DASHBOARDS="$ROOT/monitoring/grafana/dashboards" \
  PROM_URL="http://127.0.0.1:$PROM_PORT" \
  GF_PATHS_PROVISIONING="$ROOT/monitoring/grafana/provisioning" \
  GF_PATHS_DATA="$RUN/grafana-data" \
  GF_PATHS_LOGS="$RUN/logs" \
  GF_SERVER_HTTP_PORT="$GRAFANA_PORT" \
  GF_SERVER_HTTP_ADDR="127.0.0.1" \
  GF_SECURITY_ADMIN_USER=admin \
  GF_SECURITY_ADMIN_PASSWORD=admin \
  GF_AUTH_ANONYMOUS_ENABLED=true \
  GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer \
  GF_USERS_DEFAULT_THEME=dark \
  GF_ANALYTICS_REPORTING_ENABLED=false \
  GF_ANALYTICS_CHECK_FOR_UPDATES=false \
  nohup grafana server --homepath "$homepath" \
    > "$RUN/logs/grafana-stdout.log" 2>&1 &
  echo $! > "$RUN/grafana.pid"
  printf "grafana starting"
  for _ in $(seq 1 60); do
    curl -sf -m 2 "http://127.0.0.1:$GRAFANA_PORT/api/health" >/dev/null 2>&1 && { echo " -> up on :$GRAFANA_PORT"; return; }
    printf "."; sleep 1
  done
  echo " -> TIMED OUT; see $RUN/logs/grafana-stdout.log" >&2; exit 1
}

case "${1:-start}" in
  start)
    start_prometheus
    start_grafana
    echo
    echo "  grafana     http://127.0.0.1:$GRAFANA_PORT/d/token-rate-main"
    echo "  prometheus  http://127.0.0.1:$PROM_PORT"
    ;;
  stop)
    for f in "$RUN"/*.pid; do
      [[ -e "$f" ]] || continue
      pid="$(cat "$f")"
      kill "$pid" 2>/dev/null && echo "stopped $(basename "$f" .pid) (pid $pid)"
      rm -f "$f"
    done
    ;;
  status)
    curl -sf -m 2 "http://127.0.0.1:$PROM_PORT/-/healthy" >/dev/null 2>&1 \
      && echo "prometheus :$PROM_PORT  UP" || echo "prometheus :$PROM_PORT  down"
    curl -sf -m 2 "http://127.0.0.1:$GRAFANA_PORT/api/health" >/dev/null 2>&1 \
      && echo "grafana    :$GRAFANA_PORT  UP" || echo "grafana    :$GRAFANA_PORT  down"
    ;;
  *)
    echo "usage: $0 {start|stop|status}" >&2; exit 1
    ;;
esac
