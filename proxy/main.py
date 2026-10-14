"""OpenAI-compatible metering proxy.

Sits between the LangGraph swarm and the two local backends. Every request is
timed, counted, priced two ways, written to a ledger, exported to Prometheus
and (optionally) pushed to a Stripe meter -- without the caller needing to know
any of that happened.

Attribution travels in headers, not the body, so the OpenAI client libraries
work unmodified:
    X-Agent      which swarm node is spending
    X-Run-Id     which swarm execution this belongs to
    X-Customer   Stripe customer to bill
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from . import metrics, pricing
from .config import ModelRoute, load_settings
from .ledger import Ledger
from .stripe_meter import StripeMeter, billable_units

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tokenrate.proxy")

SETTINGS = load_settings()
LEDGER = Ledger(SETTINGS.ledger_db)
METER = StripeMeter(
    SETTINGS.stripe_api_key, SETTINGS.stripe_event_name, SETTINGS.stripe_enabled
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Generous read timeout: a cold 8B on a passively cooled Air can take a
    # while on the first long prompt while weights page into the GPU.
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
    log.info("profile: %s", SETTINGS.profile_name)
    log.info("routes: %s", ", ".join(SETTINGS.routes))
    log.info("local cost: $%.8f/sec", SETTINGS.cost_per_second)
    log.info("stripe: %s", "ENABLED (test mode)" if METER.enabled else "disabled")
    yield
    await app.state.http.aclose()


app = FastAPI(title="token-rate metering proxy", lifespan=lifespan)


# --------------------------------------------------------------------------- 
# helpers
# ---------------------------------------------------------------------------
class _InFlight:
    """Tracks concurrent requests so the batch size is observable.

    Used as a context manager around every upstream call, including the
    streaming path, so a client disconnecting mid-stream cannot leak the
    gauge upward and make the service look permanently busy.
    """

    def __enter__(self):
        metrics.inflight_requests.inc()
        # Read after inc so a lone request reports 1, not 0.
        self.observed = metrics.inflight_requests._value.get()
        metrics.observed_concurrency.observe(self.observed)
        return self

    def __exit__(self, *exc):
        metrics.inflight_requests.dec()
        return False


def _attribution(req: Request) -> dict:
    return {
        "agent": req.headers.get("X-Agent", "anonymous"),
        "run_id": req.headers.get("X-Run-Id"),
        "customer": req.headers.get("X-Customer", ""),
        "request_id": req.headers.get("X-Request-Id", str(uuid.uuid4())),
    }


def _extract_draft_stats(timings: dict | None) -> tuple[int | None, int | None]:
    """llama-server reports speculative-decoding counters in its timings block.

    Field names have moved around across builds, so probe a few spellings
    rather than silently reporting zero acceptance.
    """
    if not timings:
        return None, None
    drafted = timings.get("draft_n") or timings.get("n_draft")
    accepted = timings.get("draft_n_accepted") or timings.get("n_draft_accepted")
    if drafted is None and accepted is None:
        return None, None
    return (int(drafted or 0), int(accepted or 0))


def _estimate_reasoning_tokens(message: dict | None, completion_tokens: int) -> int:
    """Split billed completion tokens between hidden reasoning and visible text.

    Backends report one completion_tokens figure covering both, so apportion it
    by character length. Crude, but it reliably catches the case that matters:
    a request that returns almost nothing and bills in full.
    """
    if not message or not completion_tokens:
        return 0
    reasoning = message.get("reasoning") or message.get("reasoning_content") or ""
    if not reasoning:
        return 0
    content = message.get("content") or ""
    total_chars = len(reasoning) + len(content)
    if total_chars == 0:
        return 0
    return round(completion_tokens * len(reasoning) / total_chars)


def _observe(
    route: ModelRoute,
    attribution: dict,
    prompt_tokens: int,
    completion_tokens: int,
    ttft: float | None,
    wall: float,
    timings: dict | None,
    status: str,
    reasoning_tokens: int = 0,
) -> tuple[int, pricing.CostBreakdown]:
    labels = (route.backend, route.alias, attribution["agent"])

    cost = pricing.compute(
        SETTINGS.market_rates,
        route.rate_key,
        prompt_tokens,
        completion_tokens,
        wall,
        SETTINGS.cost_per_second,
    )

    metrics.requests_total.labels(*labels, status).inc()
    metrics.prompt_tokens_total.labels(*labels).inc(prompt_tokens)
    metrics.completion_tokens_total.labels(*labels).inc(completion_tokens)
    metrics.aggregate_completion_tokens.inc(completion_tokens)
    if reasoning_tokens:
        metrics.reasoning_tokens_total.labels(*labels).inc(reasoning_tokens)
    metrics.market_cost_usd_total.labels(*labels).inc(cost.market_total_usd)
    metrics.local_cost_usd_total.labels(*labels).inc(cost.local_total_usd)
    metrics.latency_seconds.labels(*labels).observe(wall)
    if ttft is not None:
        metrics.ttft_seconds.labels(*labels).observe(ttft)

    # Decode speed must exclude prompt processing, otherwise a long prompt
    # makes a fast backend look slow and the A/B is meaningless. When TTFT is
    # unobservable (non-streaming through a backend that reports no timings,
    # i.e. Ollama) this falls back to end-to-end rate, which understates that
    # arm. That bias is conservative for the comparison -- it can only make the
    # unspeculated control look worse -- but it is a bias, so prefer streaming
    # or the llama.cpp arms when the exact number matters.
    decode_window = max(wall - (ttft or 0.0), 1e-6)
    tps = completion_tokens / decode_window if completion_tokens else 0.0
    if completion_tokens:
        metrics.decode_tps.labels(*labels).observe(tps)

    drafted, accepted = _extract_draft_stats(timings)
    if drafted:
        metrics.draft_tokens_total.labels(route.alias).inc(drafted)
        metrics.draft_accepted_total.labels(route.alias).inc(accepted or 0)
        metrics.draft_acceptance_rate.labels(route.alias).set((accepted or 0) / drafted)

    row_id = LEDGER.record(
        request_id=attribution["request_id"],
        run_id=attribution["run_id"],
        agent=attribution["agent"],
        customer=attribution["customer"],
        backend=route.backend,
        model_alias=route.alias,
        upstream_model=route.upstream_model,
        speculative=int(route.speculative),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        ttft_seconds=ttft,
        wall_seconds=wall,
        decode_tps=tps,
        draft_n=drafted,
        draft_accepted=accepted,
        market_usd=cost.market_total_usd,
        local_usd=cost.local_total_usd,
        margin_usd=cost.margin_usd,
        status=status,
    )
    return row_id, cost


async def _bill(route: ModelRoute, attribution: dict, p_tok: int, c_tok: int, row_id: int):
    if not METER.enabled or not attribution["customer"]:
        return
    card = SETTINGS.market_rates.get(route.rate_key) or SETTINGS.market_rates["default"]
    units = billable_units(p_tok, c_tok, card["prompt"], card["completion"])
    event_id = await METER.send(
        attribution["customer"], units, idempotency_key=attribution["request_id"]
    )
    if event_id:
        LEDGER.attach_stripe_event(row_id, event_id)
        metrics.stripe_events_total.labels("ok").inc()
    else:
        metrics.stripe_events_total.labels("failed").inc()


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"ok": True, "profile": SETTINGS.profile_name,
            "routes": list(SETTINGS.routes)}


@app.get("/metrics")
async def prom_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": alias,
                "object": "model",
                "owned_by": r.backend,
                "speculative": r.speculative,
                "notes": r.notes,
            }
            for alias, r in SETTINGS.routes.items()
        ],
    }


@app.get("/usage")
async def usage(run_id: str | None = None):
    rows = LEDGER.summary(run_id)
    return {
        "run_id": run_id,
        "profile": SETTINGS.profile_name,
        "cost_per_second_usd": SETTINGS.cost_per_second,
        "by_agent": rows,
        "totals": {
            "market_usd": round(sum(r["market_usd"] or 0 for r in rows), 6),
            "local_usd": round(sum(r["local_usd"] or 0 for r in rows), 6),
            "completion_tokens": sum(r["completion_tokens"] or 0 for r in rows),
        },
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    attribution = _attribution(request)

    alias = body.get("model", "")
    route = SETTINGS.routes.get(alias)
    if route is None:
        return JSONResponse(
            {"error": {"message": f"unknown model '{alias}'", "type": "invalid_request"}},
            status_code=404,
        )

    upstream_body = dict(body)
    upstream_body["model"] = route.upstream_model
    # Ask for usage on the stream and for llama.cpp's per-request timings; both
    # are ignored by backends that don't implement them.
    if upstream_body.get("stream"):
        upstream_body["stream_options"] = {"include_usage": True}
    upstream_body.setdefault("timings_per_token", True)

    url = f"{route.base_url}/v1/chat/completions"
    client: httpx.AsyncClient = request.app.state.http

    if upstream_body.get("stream"):
        return StreamingResponse(
            _stream(client, url, upstream_body, route, attribution),
            media_type="text/event-stream",
        )

    start = time.perf_counter()
    with _InFlight():
        try:
            resp = await client.post(url, json=upstream_body)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            wall = time.perf_counter() - start
            _observe(route, attribution, 0, 0, None, wall, None, "error")
            log.error("upstream %s failed: %s", route.backend, exc)
            return JSONResponse(
                {"error": {"message": str(exc), "type": "upstream_error"}},
                status_code=502,
            )
        wall = time.perf_counter() - start
    data = resp.json()
    usage_obj = data.get("usage") or {}
    p_tok = int(usage_obj.get("prompt_tokens", 0))
    c_tok = int(usage_obj.get("completion_tokens", 0))
    timings = data.get("timings")

    # Non-streaming has no observable TTFT; llama.cpp hands us the real prompt
    # evaluation time, which is the honest equivalent.
    ttft = None
    if timings and timings.get("prompt_ms") is not None:
        ttft = float(timings["prompt_ms"]) / 1000.0

    message = (data.get("choices") or [{}])[0].get("message")
    reasoning_tok = _estimate_reasoning_tokens(message, c_tok)

    row_id, cost = _observe(
        route, attribution, p_tok, c_tok, ttft, wall, timings, "ok",
        reasoning_tokens=reasoning_tok,
    )
    await _bill(route, attribution, p_tok, c_tok, row_id)

    data["x_tokenrate"] = {
        "backend": route.backend,
        "speculative": route.speculative,
        "wall_seconds": round(wall, 4),
        "reasoning_tokens_estimated": reasoning_tok,
        **{k: round(v, 8) if isinstance(v, float) else v for k, v in cost.as_dict().items()},
    }
    return JSONResponse(data)


async def _stream(client, url, body, route: ModelRoute, attribution: dict):
    start = time.perf_counter()
    ttft: float | None = None
    p_tok = c_tok = 0
    chunk_count = 0
    reasoning_chars = content_chars = 0
    timings: dict | None = None

    # try/finally rather than a `with` block: wrapping the whole streaming body
    # would need an extra indent level across 40 lines, and a generator that is
    # closed early (client disconnect) must still decrement the gauge.
    tracker = _InFlight()
    tracker.__enter__()
    try:
        async with client.stream("POST", url, json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                yield f"{line}\n\n"
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if payload == "[DONE]":
                    continue
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                choices = obj.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    reasoning_delta = (
                        delta.get("reasoning") or delta.get("reasoning_content") or ""
                    )
                    if reasoning_delta:
                        reasoning_chars += len(reasoning_delta)
                        chunk_count += 1
                    if delta.get("content"):
                        content_chars += len(delta["content"])
                        # First *visible* token is what a user perceives as TTFT;
                        # time spent thinking is latency they still wait through,
                        # so measure from the first token of either kind.
                        chunk_count += 1
                    if (reasoning_delta or delta.get("content")) and ttft is None:
                        ttft = time.perf_counter() - start

                if obj.get("usage"):
                    p_tok = int(obj["usage"].get("prompt_tokens", 0))
                    c_tok = int(obj["usage"].get("completion_tokens", 0))
                if obj.get("timings"):
                    timings = obj["timings"]
    except Exception as exc:  # noqa: BLE001
        wall = time.perf_counter() - start
        _observe(route, attribution, p_tok, c_tok, ttft, wall, timings, "error")
        log.error("stream from %s failed: %s", route.backend, exc)
        yield f'data: {{"error": {json.dumps(str(exc))}}}\n\n'
        return
    finally:
        tracker.__exit__(None, None, None)

    wall = time.perf_counter() - start
    # Backends that omit a usage chunk still gave us one delta per token.
    if c_tok == 0:
        c_tok = chunk_count
    total_chars = reasoning_chars + content_chars
    reasoning_tok = (
        round(c_tok * reasoning_chars / total_chars) if total_chars and reasoning_chars else 0
    )
    row_id, _ = _observe(route, attribution, p_tok, c_tok, ttft, wall, timings, "ok",
                         reasoning_tokens=reasoning_tok)
    await _bill(route, attribution, p_tok, c_tok, row_id)
