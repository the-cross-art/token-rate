"""Turns a finished request into money, two different ways.

market -> counterfactual: what a hosted API would have billed for these tokens.
local  -> actual: amortized hardware + marginal electricity for the wall-clock
          seconds this machine was busy producing them.

The two diverge in an instructive way. Market cost is linear in token count and
completely indifferent to how fast you generate. Local cost is linear in *time*,
so it falls as throughput rises -- which is exactly the lever that speculative
decoding pulls. A request that market-prices at a fixed $X costs you less in
real terms the faster you can finish it.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class CostBreakdown:
    market_prompt_usd: float
    market_completion_usd: float
    market_total_usd: float
    local_total_usd: float
    local_usd_per_1k_completion: float
    margin_usd: float           # market - local; the "spread" you'd earn reselling
    margin_pct: float

    def as_dict(self) -> dict:
        return asdict(self)


def market_cost(
    rates: dict[str, dict[str, float]],
    rate_key: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> tuple[float, float]:
    card = rates.get(rate_key) or rates["default"]
    return (
        prompt_tokens / 1000.0 * card["prompt"],
        completion_tokens / 1000.0 * card["completion"],
    )


def compute(
    rates: dict[str, dict[str, float]],
    rate_key: str,
    prompt_tokens: int,
    completion_tokens: int,
    wall_seconds: float,
    cost_per_second: float,
) -> CostBreakdown:
    p_usd, c_usd = market_cost(rates, rate_key, prompt_tokens, completion_tokens)
    market_total = p_usd + c_usd

    local_total = wall_seconds * cost_per_second
    per_1k = (local_total / completion_tokens * 1000.0) if completion_tokens else 0.0

    margin = market_total - local_total
    margin_pct = (margin / market_total * 100.0) if market_total > 0 else 0.0

    return CostBreakdown(
        market_prompt_usd=p_usd,
        market_completion_usd=c_usd,
        market_total_usd=market_total,
        local_total_usd=local_total,
        local_usd_per_1k_completion=per_1k,
        margin_usd=margin,
        margin_pct=margin_pct,
    )
