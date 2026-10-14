#!/usr/bin/env bash
# Bring up one inference arm. Only ONE llama.cpp arm runs at a time: two 8B+
# servers will not share memory gracefully, and even where they fit they
# contend for the same GPU, which measures scheduling noise instead of
# decoding strategy.
#
#   ./scripts/serve.sh spec | ngram | plain | stop | status
#
# Models, context and speculation settings all come from the active hardware
# profile (config/profiles.yaml), so this script is identical on every machine.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
[[ -x "$PY" ]] || PY="python3"

prof() { "$PY" "$ROOT/scripts/profile.py" get "$1"; }

PROFILE_NAME="$("$PY" "$ROOT/scripts/profile.py" name)"
TARGET="$ROOT/models/$(prof target.file)"
DRAFT="$ROOT/models/$(prof draft.file)"
CTX="$(prof context)"
DRAFT_N_MAX="$(prof spec_draft_n_max)"
DRAFT_P_MIN="$(prof spec_draft_p_min)"

LOGDIR="$ROOT/data/logs"; mkdir -p "$LOGDIR"

common=(
  -m "$TARGET"
  --alias qwen3
  -c "$CTX"
  -ngl 99
  --flash-attn on
  --jinja                 # Qwen3 needs its chat template for tools / thinking
  --host 127.0.0.1
  --metrics
  --no-warmup
)

require_model() {
  [[ -f "$1" ]] || {
    echo "missing model: $1" >&2
    echo "run: ./scripts/fetch_models.sh" >&2
    exit 1
  }
}

start() {
  local arm="$1" port="$2"; shift 2
  require_model "$TARGET"
  if lsof -ti ":$port" >/dev/null 2>&1; then
    echo "port $port already in use; run './scripts/serve.sh stop' first" >&2; exit 1
  fi
  echo "profile: $PROFILE_NAME   model: $(basename "$TARGET")   ctx: $CTX"
  echo "starting arm '$arm' on :$port  (log: $LOGDIR/$arm.log)"
  nohup llama-server "${common[@]}" --port "$port" "$@" > "$LOGDIR/$arm.log" 2>&1 &
  echo $! > "$LOGDIR/$arm.pid"
  printf "waiting for health"
  for _ in $(seq 1 600); do
    if curl -sf "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
      echo " -> up"; return 0
    fi
    printf "."; sleep 1
  done
  echo " -> TIMED OUT; see $LOGDIR/$arm.log" >&2; exit 1
}

case "${1:-}" in
  spec)
    require_model "$DRAFT"
    # p-min gates on drafter confidence: without it a hesitant drafter spends
    # target-model compute on tokens that get rejected. Watch the acceptance
    # rate -- below ~0.6 the drafter is costing more than it saves.
    start spec 8081 \
      --spec-type draft-simple \
      -md "$DRAFT" -ngld 99 \
      --spec-draft-n-max "$DRAFT_N_MAX" \
      --spec-draft-n-min 1 \
      --spec-draft-p-min "$DRAFT_P_MIN"
    ;;
  ngram)
    # No draft model, no extra memory. Wins on repetitive or quote-heavy
    # output, near-zero gain on novel prose.
    start ngram 8082 --spec-type ngram-mod
    ;;
  plain)
    start plain 8083
    ;;
  stop)
    for f in "$LOGDIR"/*.pid; do
      [[ -e "$f" ]] || continue
      pid="$(cat "$f")"
      kill "$pid" 2>/dev/null && echo "stopped $(basename "$f" .pid) (pid $pid)"
      rm -f "$f"
    done
    ;;
  status)
    echo "profile: $PROFILE_NAME"
    for p in 8081 8082 8083 11434 8080 9090 3001; do
      # Each service spells its health endpoint differently: llama.cpp /health,
      # the proxy /healthz, Ollama /api/version, Grafana /api/health and
      # Prometheus /-/healthy. Probe them all rather than reporting a live
      # service as down.
      if curl -sf -m 2 "http://127.0.0.1:$p/health"      >/dev/null 2>&1 \
      || curl -sf -m 2 "http://127.0.0.1:$p/healthz"     >/dev/null 2>&1 \
      || curl -sf -m 2 "http://127.0.0.1:$p/api/version" >/dev/null 2>&1 \
      || curl -sf -m 2 "http://127.0.0.1:$p/api/health"  >/dev/null 2>&1 \
      || curl -sf -m 2 "http://127.0.0.1:$p/-/healthy"   >/dev/null 2>&1; then
        echo ":$p  UP"
      else
        echo ":$p  down"
      fi
    done
    ;;
  *)
    echo "usage: $0 {spec|ngram|plain|stop|status}" >&2; exit 1
    ;;
esac
