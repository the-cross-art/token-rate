"""Multi-tenant load generator. Finds the batch ceiling that sets your margin.

Decoding is memory-bandwidth bound: producing one token means reading every
weight in the model. Serving N requests from a single weight read is what makes
tokens cheap, so a server's cost per token is set almost entirely by how many
requests it serves concurrently -- not by how fast it serves one.

    batch 1   -> one token per weight read
    batch 64  -> 64 tokens per weight read, same seconds billed

This tool drives concurrent load through the metering proxy and reports
AGGREGATE throughput and cost per million tokens at each level. The point where
aggregate tokens/sec stops climbing is your batch ceiling, and that number is
the floor under any price you can charge.

    python bench/load.py --sweep                      # find the ceiling
    python bench/load.py --concurrency 32 --duration 60
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parent.parent
PROXY = "http://127.0.0.1:8080"
console = Console()

# Realistic tenant mix. Prompt length matters as much as output length: a RAG
# tenant sending 4k-token prompts consumes prefill compute that a chat tenant
# never touches, and prefill is compute-bound where decode is bandwidth-bound.
# A load test using one shape will mis-size your service.
WORKLOADS = {
    "chat": {
        "weight": 0.55,
        "prompt": "Answer in two sentences: {q}",
        "padding_tokens": 0,
        "max_tokens": 120,
    },
    "rag": {
        "weight": 0.30,
        "prompt": "Context:\n{pad}\n\nUsing only the context, answer: {q}",
        "padding_tokens": 2000,
        "max_tokens": 250,
    },
    "generate": {
        "weight": 0.15,
        "prompt": "Write a detailed technical explanation of: {q}",
        "padding_tokens": 0,
        "max_tokens": 600,
    },
}

QUESTIONS = [
    "how KV caching reduces redundant computation",
    "why quantization trades accuracy for memory bandwidth",
    "what continuous batching does to tail latency",
    "how speculative decoding verifies drafted tokens",
    "why prefill is compute-bound but decode is memory-bound",
    "what paged attention solves in KV cache management",
]

# ~4 characters per token is close enough for Qwen3 on English prose, and this
# only needs to produce a prompt of roughly the right size.
_PAD_WORD = "context "


@dataclass
class Result:
    tenant: str
    workload: str
    ok: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0
    wall: float = 0.0
    ttft: float | None = None
    local_usd: float = 0.0
    market_usd: float = 0.0
    error: str = ""


@dataclass
class Stats:
    concurrency: int
    results: list[Result] = field(default_factory=list)
    duration: float = 0.0

    @property
    def ok(self) -> list[Result]:
        return [r for r in self.results if r.ok]

    @property
    def aggregate_tps(self) -> float:
        """Total output tokens per second across ALL concurrent requests.

        This is the number that scales with batch size and the one your unit
        cost is derived from. Per-request speed falls as concurrency rises;
        aggregate speed is what matters.
        """
        total = sum(r.completion_tokens for r in self.ok)
        return total / self.duration if self.duration else 0.0

    @property
    def per_request_tps(self) -> float:
        vals = [r.completion_tokens / r.wall for r in self.ok if r.wall > 0]
        return statistics.median(vals) if vals else 0.0

    def latency(self, pct: float) -> float:
        vals = sorted(r.wall for r in self.ok)
        if not vals:
            return 0.0
        return vals[min(int(len(vals) * pct), len(vals) - 1)]


def build_prompt(workload: str) -> tuple[str, int]:
    spec = WORKLOADS[workload]
    pad = _PAD_WORD * (spec["padding_tokens"] // 2) if spec["padding_tokens"] else ""
    q = random.choice(QUESTIONS)
    return spec["prompt"].format(q=q, pad=pad), spec["max_tokens"]


def pick_workload() -> str:
    r, acc = random.random(), 0.0
    for name, spec in WORKLOADS.items():
        acc += spec["weight"]
        if r <= acc:
            return name
    return "chat"


async def one_request(
    client: httpx.AsyncClient, model: str, tenant: str, customers: dict
) -> Result:
    workload = pick_workload()
    prompt, max_tokens = build_prompt(workload)
    headers = {"X-Agent": tenant, "X-Run-Id": "load"}
    if customers.get(tenant):
        headers["X-Customer"] = customers[tenant]

    t0 = time.perf_counter()
    try:
        resp = await client.post(
            f"{PROXY}/v1/chat/completions",
            headers=headers,
            json={
                "model": model,
                "max_tokens": max_tokens,
                "temperature": 0.7,
                "messages": [{"role": "user", "content": prompt + " /no_think"}],
            },
        )
        resp.raise_for_status()
        d = resp.json()
    except Exception as exc:  # noqa: BLE001
        return Result(tenant, workload, False, wall=time.perf_counter() - t0,
                      error=str(exc)[:120])

    u = d.get("usage", {})
    x = d.get("x_tokenrate", {})
    return Result(
        tenant=tenant, workload=workload, ok=True,
        prompt_tokens=u.get("prompt_tokens", 0),
        completion_tokens=u.get("completion_tokens", 0),
        wall=x.get("wall_seconds", time.perf_counter() - t0),
        local_usd=x.get("local_total_usd", 0.0),
        market_usd=x.get("market_total_usd", 0.0),
    )


async def run_level(
    model: str, concurrency: int, duration: float, tenants: int, customers: dict
) -> Stats:
    """Hold `concurrency` requests in flight for `duration` seconds."""
    stats = Stats(concurrency=concurrency)
    stop_at = time.perf_counter() + duration
    limits = httpx.Limits(max_connections=concurrency + 8,
                          max_keepalive_connections=concurrency + 8)

    async with httpx.AsyncClient(timeout=httpx.Timeout(900.0), limits=limits) as client:
        t0 = time.perf_counter()

        async def worker(idx: int):
            # Each worker is a distinct tenant, so per-customer billing is
            # exercised under concurrency -- that is where meters double-count.
            tenant = f"tenant-{idx % tenants:02d}"
            while time.perf_counter() < stop_at:
                stats.results.append(
                    await one_request(client, model, tenant, customers)
                )

        await asyncio.gather(*[worker(i) for i in range(concurrency)])
        stats.duration = time.perf_counter() - t0

    return stats


def report(levels: list[Stats], cost_per_hour: float) -> None:
    t = Table(title="scaling curve — aggregate throughput vs concurrency",
              header_style="bold")
    for c, j in [("batch", "right"), ("reqs", "right"), ("aggregate tok/s", "right"),
                 ("per-req tok/s", "right"), ("p50 lat", "right"),
                 ("p99 lat", "right"), ("$/1M tokens", "right"), ("vs batch 1", "right")]:
        t.add_column(c, justify=j)

    base = levels[0].aggregate_tps if levels else 0.0
    for s in levels:
        agg = s.aggregate_tps
        per_m = (cost_per_hour / (agg * 3600) * 1_000_000) if agg else 0.0
        speedup = f"{agg / base:.1f}x" if base else "—"
        t.add_row(
            str(s.concurrency), str(len(s.ok)), f"{agg:,.1f}",
            f"{s.per_request_tps:.1f}", f"{s.latency(0.50):.1f}s",
            f"{s.latency(0.99):.1f}s", f"${per_m:,.2f}", speedup,
        )
    console.print(t)

    if len(levels) >= 2:
        best = max(levels, key=lambda s: s.aggregate_tps)
        console.print(
            f"\n[bold]batch ceiling[/]: aggregate throughput peaks at "
            f"concurrency [bold]{best.concurrency}[/] "
            f"({best.aggregate_tps:,.1f} tok/s)."
        )
        per_m = (cost_per_hour / (best.aggregate_tps * 3600) * 1_000_000)
        console.print(
            f"[bold]floor price[/]: ${per_m:,.2f} per 1M output tokens at that point.\n"
            f"[dim]Anything you charge above this is margin; below it you lose money "
            f"on every token.[/]"
        )
        errs = [r for s in levels for r in s.results if not r.ok]
        if errs:
            console.print(f"[yellow]{len(errs)} request(s) failed[/] — "
                          f"first: {errs[0].error}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="spec-qwen3-8b")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--duration", type=float, default=30.0,
                    help="seconds to hold each concurrency level")
    ap.add_argument("--tenants", type=int, default=8,
                    help="distinct billing identities to spread load across")
    ap.add_argument("--sweep", action="store_true",
                    help="ramp concurrency 1,2,4,8,16,32 and find the batch ceiling")
    ap.add_argument("--levels", type=int, nargs="*",
                    default=[1, 2, 4, 8, 16, 32])
    ap.add_argument("--out", default="data/load.json")
    args = ap.parse_args()

    try:
        health = httpx.get(f"{PROXY}/healthz", timeout=3).json()
    except Exception:
        console.print(f"[red]proxy not reachable at {PROXY}[/] — run 'make proxy'")
        return 1
    console.print(f"profile [bold]{health.get('profile')}[/], "
                  f"model [bold]{args.model}[/]")

    usage = httpx.get(f"{PROXY}/usage", timeout=10).json()
    cost_per_hour = usage["cost_per_second_usd"] * 3600
    console.print(f"cost basis [bold]${cost_per_hour:.4f}/hour[/]\n")

    state = ROOT / "data" / "stripe.json"
    customers = json.loads(state.read_text()).get("customers", {}) if state.exists() else {}

    levels_to_run = args.levels if args.sweep else [args.concurrency]
    results: list[Stats] = []
    for c in levels_to_run:
        console.print(f"[dim]concurrency {c} for {args.duration:.0f}s...[/]")
        s = asyncio.run(run_level(args.model, c, args.duration, args.tenants, customers))
        console.print(f"  {len(s.ok)} ok, aggregate {s.aggregate_tps:,.1f} tok/s")
        results.append(s)

    console.print()
    report(results, cost_per_hour)

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([{
        "concurrency": s.concurrency, "duration": s.duration,
        "requests": len(s.ok), "aggregate_tps": s.aggregate_tps,
        "per_request_tps": s.per_request_tps,
        "p50": s.latency(0.50), "p99": s.latency(0.99),
    } for s in results], indent=2))
    console.print(f"\n[dim]raw results -> {out}[/]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
