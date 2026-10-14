"""Cycle every arm over an identical prompt set and report the economics.

Arms are started and stopped one at a time: 16GB cannot hold two 8B servers,
and running them concurrently would make them contend for the same GPU, which
would measure scheduling noise rather than speculative decoding.

Prompt sets matter more than people expect. Draft-model speculation wins on
predictable continuations; n-gram speculation wins when the output quotes the
input. A single prompt type will tell you a flattering or damning story about
either one, so this runs three deliberately different kinds.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parent.parent
PROXY = "http://127.0.0.1:8080"
console = Console()

# arm alias -> (serve.sh target, port). ollama is managed separately.
ARMS = {
    "spec-qwen3-8b": ("spec", 8081),
    "ngram-qwen3-8b": ("ngram", 8082),
    "plain-qwen3-8b": ("plain", 8083),
    "ollama-qwen3-8b": (None, 11434),
}

PROMPTS = {
    # Novel prose: the hard case. The drafter has to actually predict.
    "generative": "Explain, in about 250 words, why unified memory bandwidth "
                  "rather than raw FLOPS is the binding constraint when running "
                  "a quantized 8B model on Apple silicon.",
    # Structured output: highly predictable token sequences, draft models shine.
    "structured": "Output a JSON array of 12 objects, each with keys "
                  "\"id\", \"name\", \"status\", \"latency_ms\". Status is one of "
                  "\"ok\", \"degraded\", \"down\". Output only JSON.",
    # Quote-heavy: the n-gram arm's best case, since output repeats the input.
    "extractive": "Here is a config file:\n\n"
                  "host=127.0.0.1\nport=8081\nctx=8192\nngl=99\ndraft_max=6\n"
                  "draft_p_min=0.75\nflash_attn=on\nmetrics=true\n\n"
                  "Repeat every line back verbatim, then append a one-line "
                  "comment after each explaining what it does.",
}


def wait_proxy() -> None:
    for _ in range(30):
        try:
            httpx.get(f"{PROXY}/healthz", timeout=2).raise_for_status()
            return
        except Exception:
            time.sleep(1)
    console.print("[red]proxy not reachable[/]; start it with 'make proxy'")
    sys.exit(1)


def serve(target: str | None) -> None:
    if target is None:
        return
    subprocess.run([str(ROOT / "scripts" / "serve.sh"), "stop"], check=False,
                   capture_output=True)
    time.sleep(2)
    r = subprocess.run([str(ROOT / "scripts" / "serve.sh"), target],
                       capture_output=True, text=True)
    if r.returncode != 0:
        console.print(f"[red]failed to start arm {target}[/]\n{r.stderr}")
        sys.exit(1)


def one_call(alias: str, prompt: str, max_tokens: int) -> dict | None:
    r = httpx.post(
        f"{PROXY}/v1/chat/completions",
        headers={"X-Agent": "bench", "X-Run-Id": "bench"},
        json={"model": alias, "max_tokens": max_tokens, "temperature": 0.0,
              "messages": [{"role": "user", "content": prompt + " /no_think"}]},
        timeout=600,
    )
    if r.status_code != 200:
        console.print(f"[yellow]{alias}: HTTP {r.status_code} {r.text[:160]}[/]")
        return None
    return r.json()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3, help="repetitions per prompt")
    ap.add_argument("--max-tokens", type=int, default=400)
    ap.add_argument("--arms", nargs="*", default=list(ARMS))
    ap.add_argument("--out", default="data/bench.json")
    args = ap.parse_args()

    wait_proxy()
    results: list[dict] = []

    for alias in args.arms:
        target, port = ARMS[alias]
        console.rule(f"[bold]{alias}")
        serve(target)
        # One untimed call to page weights into the GPU and warm the KV cache;
        # including it would charge the first arm for everyone's cold start.
        console.print("[dim]warmup...[/]")
        one_call(alias, "Say OK.", 8)

        for kind, prompt in PROMPTS.items():
            for rep in range(args.reps):
                data = one_call(alias, prompt, args.max_tokens)
                if not data:
                    continue
                tr = data.get("x_tokenrate", {})
                u = data.get("usage", {})
                wall = tr.get("wall_seconds", 0.0)
                out_tok = u.get("completion_tokens", 0)
                results.append({
                    "arm": alias, "prompt": kind, "rep": rep,
                    "prompt_tokens": u.get("prompt_tokens", 0),
                    "completion_tokens": out_tok,
                    "wall_seconds": wall,
                    "tps": out_tok / wall if wall else 0.0,
                    "market_usd": tr.get("market_total_usd", 0.0),
                    "local_usd": tr.get("local_total_usd", 0.0),
                    "local_usd_per_1k": tr.get("local_usd_per_1k_completion", 0.0),
                })
                console.print(f"  {kind:<11} rep{rep}  "
                              f"{results[-1]['tps']:6.1f} tok/s  "
                              f"${results[-1]['local_usd']:.6f}")

    subprocess.run([str(ROOT / "scripts" / "serve.sh"), "stop"], check=False,
                   capture_output=True)

    outp = ROOT / args.out
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(results, indent=2))

    # ---- report -----------------------------------------------------------
    console.rule("[bold]results")
    t = Table(header_style="bold")
    t.add_column("arm"); t.add_column("prompt")
    for c in ("tok/s (median)", "out tok", "local $/1k out", "vs control"):
        t.add_column(c, justify="right")

    control = {}
    for kind in PROMPTS:
        rows = [r for r in results if r["arm"] == "plain-qwen3-8b" and r["prompt"] == kind]
        if rows:
            control[kind] = statistics.median(r["tps"] for r in rows)

    for alias in args.arms:
        for kind in PROMPTS:
            rows = [r for r in results if r["arm"] == alias and r["prompt"] == kind]
            if not rows:
                continue
            tps = statistics.median(r["tps"] for r in rows)
            per1k = statistics.median(r["local_usd_per_1k"] for r in rows)
            base = control.get(kind)
            delta = f"{(tps / base - 1) * 100:+.0f}%" if base else "—"
            t.add_row(alias, kind, f"{tps:.1f}",
                      f"{statistics.median(r['completion_tokens'] for r in rows):.0f}",
                      f"${per1k:.5f}", delta)
    console.print(t)
    console.print(f"\n[dim]raw results -> {outp}[/]")
    console.print("[dim]'vs control' compares median decode speed against the "
                  "unspeculated llama.cpp arm on the same prompt.[/]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
