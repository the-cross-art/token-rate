"""Create the Stripe test-mode objects this project bills against.

Creates, idempotently by lookup key / name:
  1. a billing Meter        aggregates `llm_tokens` events, summed per customer
  2. a Product + metered Price  $/1000 tokens, driven by that meter
  3. one Customer per swarm agent, each subscribed to the price

Test mode only -- the script refuses a live key. Nothing here can charge money:
test-mode customers have no real payment method and invoices never settle.

    python scripts/stripe_bootstrap.py            # create / show
    python scripts/stripe_bootstrap.py --invoice  # preview current usage as an invoice
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

import stripe  # noqa: E402

KEY = os.environ.get("STRIPE_API_KEY", "")
EVENT_NAME = os.environ.get("STRIPE_METER_EVENT_NAME", "llm_tokens")
STATE = ROOT / "data" / "stripe.json"

# Must match config/rates.yaml qwen3-8b completion rate: the meter carries
# completion-equivalent units, so the price is the completion rate per 1k.
USD_PER_1K_TOKENS = 0.00054

AGENTS = ["planner", "researcher-1", "researcher-2", "researcher-3",
          "critic", "synthesizer", "bench"]


def require_test_key() -> None:
    if not KEY:
        sys.exit("STRIPE_API_KEY is unset. Put a test key in .env.")
    if not KEY.startswith("sk_test_"):
        sys.exit("Refusing to run: STRIPE_API_KEY is not a test-mode key "
                 "(sk_test_...). This project never touches live mode.")
    stripe.api_key = KEY


def get_or_create_meter():
    for m in stripe.billing.Meter.list(limit=100).auto_paging_iter():
        if m.event_name == EVENT_NAME and m.status == "active":
            print(f"meter exists: {m.id}")
            return m
    m = stripe.billing.Meter.create(
        display_name="LLM tokens",
        event_name=EVENT_NAME,
        default_aggregation={"formula": "sum"},
        customer_mapping={"type": "by_id", "event_payload_key": "stripe_customer_id"},
        value_settings={"event_payload_key": "value"},
    )
    print(f"meter created: {m.id}")
    return m


def get_or_create_price(meter_id: str):
    lookup = "tokenrate_qwen3_8b_per_1k"
    existing = stripe.Price.list(lookup_keys=[lookup], limit=1)
    if existing.data:
        print(f"price exists: {existing.data[0].id}")
        return existing.data[0]

    product = stripe.Product.create(name="Token Rate — local Qwen3 inference")
    price = stripe.Price.create(
        product=product.id,
        lookup_key=lookup,
        currency="usd",
        recurring={"interval": "month", "usage_type": "metered", "meter": meter_id},
        # Stripe rejects `tiers` together with `transform_quantity`, so this
        # uses the simpler per-unit form: the meter reports raw tokens,
        # transform_quantity divides them into thousands, and unit_amount is
        # the price of one thousand. unit_amount_decimal is in CENTS, hence
        # the *100.
        billing_scheme="per_unit",
        unit_amount_decimal=str(USD_PER_1K_TOKENS * 100),
        transform_quantity={"divide_by": 1000, "round": "up"},
    )
    print(f"price created: {price.id}  (${USD_PER_1K_TOKENS}/1k tokens)")
    return price


def get_or_create_customers(price_id: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for agent in AGENTS:
        email = f"{agent}@token-rate.test"
        found = stripe.Customer.list(email=email, limit=1)
        if found.data:
            cust = found.data[0]
        else:
            cust = stripe.Customer.create(
                name=f"swarm/{agent}", email=email,
                description="token-rate test-mode agent",
                metadata={"agent": agent},
            )
            # A subscription is what turns meter events into invoice line items.
            stripe.Subscription.create(
                customer=cust.id,
                items=[{"price": price_id}],
                # No payment method exists in test mode; this keeps the
                # subscription active instead of failing at first invoice.
                collection_method="send_invoice",
                days_until_due=30,
            )
        out[agent] = cust.id
        print(f"  {agent:<14} {cust.id}")
    return out


def show_invoices(state: dict) -> None:
    """Read accrued usage back out of Stripe, per agent.

    This is the reconciliation step: these numbers come from Stripe's meter,
    not from the local ledger, so a disagreement between this and
    `make usage` means events were dropped or double-counted.
    """
    now = int(time.time())
    # Meter summary windows must be aligned to the hour.
    end = now - (now % 3600) + 3600
    start = end - 86400 * 7

    print("\naccrued billable usage (last 7 days, from Stripe):")
    grand = 0.0
    for agent, cid in state["customers"].items():
        try:
            summaries = stripe.billing.Meter.list_event_summaries(
                state["meter"],
                customer=cid,
                start_time=start,
                end_time=end,
                value_grouping_window="hour",
            )
            units = sum(s.aggregated_value for s in summaries.auto_paging_iter())
        except Exception as exc:  # noqa: BLE001
            print(f"  {agent:<14} unavailable: {exc}")
            continue
        cost = units / 1000 * USD_PER_1K_TOKENS
        grand += cost
        print(f"  {agent:<14} {units:>10,.0f} units   ${cost:.6f}")
    print(f"  {'TOTAL':<14} {'':>10}   ${grand:.6f}")
    print("\nStripe aggregates meter events asynchronously; allow ~a minute "
          "after a run before these settle.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--invoice", action="store_true",
                    help="show accrued usage per agent instead of creating objects")
    args = ap.parse_args()
    require_test_key()

    if args.invoice:
        if not STATE.exists():
            sys.exit("run without --invoice first")
        show_invoices(json.loads(STATE.read_text()))
        return 0

    meter = get_or_create_meter()
    price = get_or_create_price(meter.id)
    print("customers:")
    customers = get_or_create_customers(price.id)

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(
        {"meter": meter.id, "event_name": EVENT_NAME, "price": price.id,
         "customers": customers}, indent=2))
    print(f"\nwrote {STATE}")
    print("\nNext: set STRIPE_ENABLED=true in .env, restart the proxy, then run")
    print("the swarm with STRIPE_CUSTOMER_ID set, or let bench/swarm pass")
    print("X-Customer per agent. Usage appears in the Stripe test dashboard")
    print("under Billing > Meters within ~a minute.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
