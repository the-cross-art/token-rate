"""Run the swarm once and print the cost breakdown the proxy recorded."""
from __future__ import annotations

import argparse
import sys
import time
import uuid

import httpx
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()
from .graph import build_graph  # noqa: E402

console = Console()
PROXY = "http://127.0.0.1:8080"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("topic", nargs="?",
                    default="The real cost drivers of self-hosting a quantized "
                            "8B language model versus using a hosted API")
    ap.add_argument("--show-report", action="store_true")
    args = ap.parse_args()

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    console.print(f"[bold]run[/] {run_id}\n[bold]topic[/] {args.topic}\n")

    try:
        httpx.get(f"{PROXY}/healthz", timeout=3).raise_for_status()
    except Exception:
        console.print(f"[red]proxy not reachable at {PROXY}[/] — start it with "
                      f"'make proxy'")
        return 1

    graph = build_graph()
    t0 = time.perf_counter()
    with console.status("running swarm..."):
        result = graph.invoke({"topic": args.topic, "run_id": run_id,
                               "angles": [], "findings": [], "critique": "",
                               "report": ""})
    wall = time.perf_counter() - t0
    console.print(f"[green]done[/] in {wall:.1f}s\n")

    if args.show_report:
        console.rule("report")
        console.print(result["report"])
        console.rule()

    usage = httpx.get(f"{PROXY}/usage", params={"run_id": run_id}, timeout=10).json()

    t = Table(title=f"cost breakdown — {run_id}", header_style="bold")
    for col in ("agent", "reqs", "prompt tok", "out tok", "tok/s", "ttft",
                "hosted $", "local $"):
        t.add_column(col, justify="right" if col != "agent" else "left")
    for r in usage["by_agent"]:
        t.add_row(r["agent"], str(r["requests"]), f'{r["prompt_tokens"]:,}',
                  f'{r["completion_tokens"]:,}', f'{r["avg_tps"] or 0:.1f}',
                  f'{r["avg_ttft"] or 0:.2f}s', f'${r["market_usd"]:.6f}',
                  f'${r["local_usd"]:.6f}')
    console.print(t)

    tot = usage["totals"]
    saved = tot["market_usd"] - tot["local_usd"]
    console.print(
        f'\nhosted-equivalent [bold]${tot["market_usd"]:.6f}[/]   '
        f'actual local [bold]${tot["local_usd"]:.6f}[/]   '
        f'spread [bold green]${saved:.6f}[/]'
    )
    console.print(f'[dim]machine cost basis: ${usage["cost_per_second_usd"]*3600:.4f}/hour[/]')
    return 0


if __name__ == "__main__":
    sys.exit(main())
