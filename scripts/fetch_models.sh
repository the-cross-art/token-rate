#!/usr/bin/env bash
# Download the GGUF files the active hardware profile needs.
#
# Resumable and idempotent: re-running skips files already at full size, and an
# interrupted download continues rather than restarting. Hugging Face serves
# these through a CDN that drops long HTTP/2 transfers, so this forces HTTP/1.1
# and retries on a stall -- a 20GB file over a flaky link will otherwise fail
# repeatedly at different offsets.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"; [[ -x "$PY" ]] || PY="python3"
MODELS="$ROOT/models"; mkdir -p "$MODELS"

prof() { "$PY" "$ROOT/scripts/profile.py" get "$1"; }
echo "profile: $("$PY" "$ROOT/scripts/profile.py" name)"

fetch() {
  local repo="$1" file="$2" approx_gb="$3" dest="$MODELS/$2"
  local url="https://huggingface.co/${repo}/resolve/main/${file}"

  local expected_bytes
  expected_bytes="$(curl -sIL "$url" -o /dev/null -w '%{size_download}' || echo 0)"
  # A HEAD through the CDN often reports 0; fall back to the profile's estimate.
  if [[ "$expected_bytes" -lt 1000 ]]; then
    expected_bytes="$(awk -v g="$approx_gb" 'BEGIN{printf "%d", g*1024*1024*1024*0.93}')"
  fi

  if [[ -f "$dest" ]] && [[ "$(stat -f%z "$dest" 2>/dev/null || stat -c%s "$dest")" -ge "$expected_bytes" ]]; then
    echo "  ✓ $file already complete"
    return
  fi

  echo "  ↓ $file  (~${approx_gb}GB)"
  for attempt in $(seq 1 15); do
    if curl -L --http1.1 --retry 5 --retry-all-errors -C - \
            --speed-limit 2048 --speed-time 60 \
            --progress-bar -o "$dest" "$url"; then
      echo "  ✓ $file"
      return
    fi
    echo "    attempt $attempt stalled at $(stat -f%z "$dest" 2>/dev/null || stat -c%s "$dest") bytes; resuming"
    sleep 3
  done
  echo "  ✗ failed to download $file after 15 attempts" >&2
  exit 1
}

fetch "$(prof target.repo)" "$(prof target.file)" "$(prof target.approx_gb)"
fetch "$(prof draft.repo)"  "$(prof draft.file)"  "$(prof draft.approx_gb)"

echo
ls -lh "$MODELS"/*.gguf
