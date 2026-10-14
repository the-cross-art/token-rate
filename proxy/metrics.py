"""Prometheus instrumentation.

Every series is labelled by (backend, model, agent) so a single Grafana
dashboard can answer both "is speculative decoding faster?" and "which agent
in the swarm is burning the budget?" without a second data source.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

LABELS = ("backend", "model", "agent")

requests_total = Counter(
    "tokenrate_requests_total", "Completed inference requests.", LABELS + ("status",)
)

prompt_tokens_total = Counter(
    "tokenrate_prompt_tokens_total", "Prompt (input) tokens consumed.", LABELS
)
completion_tokens_total = Counter(
    "tokenrate_completion_tokens_total", "Completion (output) tokens produced.", LABELS
)

market_cost_usd_total = Counter(
    "tokenrate_market_cost_usd_total",
    "Counterfactual hosted-API cost of served tokens, USD.",
    LABELS,
)
local_cost_usd_total = Counter(
    "tokenrate_local_cost_usd_total",
    "Actual amortized hardware+power cost of serving, USD.",
    LABELS,
)

# Buckets chosen for a 16GB M4 running an 8B at Q4: TTFT is dominated by prompt
# processing and realistically lands between 80ms and ~4s on long contexts.
ttft_seconds = Histogram(
    "tokenrate_ttft_seconds",
    "Time to first token.",
    LABELS,
    buckets=(0.05, 0.1, 0.2, 0.4, 0.8, 1.5, 3.0, 6.0, 12.0),
)

latency_seconds = Histogram(
    "tokenrate_request_seconds",
    "Total wall-clock time per request.",
    LABELS,
    buckets=(0.25, 0.5, 1, 2, 4, 8, 16, 32, 64, 128),
)

# An 8B Q4 on M4 does roughly 18-25 tok/s unaided; speculative decoding should
# push the upper buckets. If these two backends overlap, spec decoding is not
# earning its memory.
decode_tps = Histogram(
    "tokenrate_decode_tokens_per_second",
    "Output tokens per second, excluding prompt processing.",
    LABELS,
    buckets=(5, 10, 15, 20, 25, 30, 40, 55, 75, 100, 150),
)

# Qwen3 is a hybrid-thinking model: it emits reasoning tokens that are billed
# like any other completion token but never shown to the caller. A request can
# return empty content and still cost full price. Neither llama.cpp nor Ollama
# reports a separate reasoning token count, so this is estimated from the
# character split between the reasoning and content fields -- approximate, but
# the ratio is what matters and it is the difference between a cost model that
# explains your bill and one that quietly understates it.
reasoning_tokens_total = Counter(
    "tokenrate_reasoning_tokens_estimated_total",
    "Estimated completion tokens spent on hidden reasoning.",
    LABELS,
)

# --- Speculative-decoding specific -----------------------------------------
draft_acceptance_rate = Gauge(
    "tokenrate_draft_acceptance_rate",
    "Fraction of drafted tokens accepted by the target model (last request).",
    ("model",),
)
draft_tokens_total = Counter(
    "tokenrate_draft_tokens_total", "Tokens proposed by the draft model.", ("model",)
)
draft_accepted_total = Counter(
    "tokenrate_draft_accepted_total", "Drafted tokens accepted by the target.", ("model",)
)

# --- Concurrency -----------------------------------------------------------
# These are the metrics that decide whether a token-billing service makes
# money. Decoding is memory-bandwidth bound: producing one token requires
# reading every weight in the model. Serving N requests from a single weight
# read is what makes tokens cheap, so AGGREGATE throughput -- not per-request
# speed -- is the number your margin depends on. A server at batch 1 and the
# same server at batch 64 cost the same per second and differ ~50x in cost per
# token.
inflight_requests = Gauge(
    "tokenrate_inflight_requests",
    "Requests currently being served. This is the effective batch size.",
)

# Sampled at the moment each request STARTS, so the distribution shows the
# concurrency actually achieved rather than the concurrency requested.
observed_concurrency = Histogram(
    "tokenrate_observed_concurrency",
    "In-flight request count observed at request start.",
    buckets=(1, 2, 4, 8, 16, 32, 64, 128, 256, 512),
)

# Aggregate output tokens, unlabelled, so a single PromQL rate() gives total
# service throughput without summing across label permutations.
aggregate_completion_tokens = Counter(
    "tokenrate_aggregate_completion_tokens_total",
    "All output tokens served, for whole-service throughput.",
)

queue_wait_seconds = Histogram(
    "tokenrate_queue_wait_seconds",
    "Time between accepting a request and the upstream accepting it.",
    buckets=(0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)

# --- Billing ---------------------------------------------------------------
stripe_events_total = Counter(
    "tokenrate_stripe_meter_events_total",
    "Meter events pushed to Stripe.",
    ("outcome",),
)
