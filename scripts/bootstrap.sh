#!/usr/bin/env bash
# One-command setup on a fresh machine.
#
#   ./scripts/bootstrap.sh                      # auto-detect hardware profile
#   TOKENRATE_PROFILE=m5pro-48gb ./scripts/bootstrap.sh
#
# Installs dependencies, downloads the profile's models, and verifies the
# toolchain. It does NOT start anything -- see `make help`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

step() { printf "\n\033[1m==> %s\033[0m\n" "$1"; }

step "Checking prerequisites"
command -v brew >/dev/null || { echo "Homebrew required: https://brew.sh" >&2; exit 1; }
for tool in llama-server prometheus grafana; do
  if command -v "$tool" >/dev/null; then
    echo "  ✓ $tool"
  else
    echo "  ↓ installing $tool"
    case "$tool" in
      llama-server) brew install llama.cpp ;;
      *)            brew install "$tool" ;;
    esac
  fi
done
command -v ollama >/dev/null && echo "  ✓ ollama" || {
  echo "  ↓ installing ollama"; brew install ollama; }
command -v uv >/dev/null && echo "  ✓ uv" || { echo "  ↓ installing uv"; brew install uv; }

step "Python environment"
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e .
[[ -f .env ]] || { cp .env.example .env; echo "  created .env from template"; }

step "Hardware profile"
.venv/bin/python scripts/profile.py name
.venv/bin/python - <<'PY'
import sys; sys.path.insert(0, "scripts")
import profile
name, p = profile.resolve()
print(f"  {p['description']}")
print(f"  target : {p['target']['file']}  (~{p['target']['approx_gb']}GB)")
print(f"  draft  : {p['draft']['file']}  (~{p['draft']['approx_gb']}GB)")
print(f"  context: {p['context']}")
c = p["cost"]
hourly = (c["machine_cost_usd"]/c["amortization_hours"]
          + (c["load_watts"]-c["idle_watts"])/1000*c["electricity_usd_per_kwh"])
print(f"  cost   : ${hourly:.4f}/hour  <-- UPDATE machine_cost_usd and")
print(f"           load_watts in config/profiles.yaml for your machine,")
print(f"           or every local cost number will be wrong.")
PY

step "Downloading models"
./scripts/fetch_models.sh

step "Pulling the Ollama control model"
if ! ollama list >/dev/null 2>&1; then
  nohup ollama serve >/dev/null 2>&1 &
  sleep 3
fi
ollama pull qwen3:8b || echo "  (skipped -- start 'ollama serve' and retry)"

step "Done"
cat <<'EOF'

Start it in four shells:

  make monitoring     # Prometheus :9090 + Grafana :3001   (native, ~420MB)
  make proxy          # metering proxy :8080               (leave running)
  make spec           # speculative arm :8081              (leave running)
  make load           # ramp concurrency, find the batch ceiling

Then:  http://127.0.0.1:3001/d/token-rate-main

On a machine with RAM to spare, `make monitoring-docker` runs the same
dashboards under Docker Desktop instead.

Optional metered billing (Stripe test mode, free, cannot charge):
  see README.md "Metered billing"
EOF
