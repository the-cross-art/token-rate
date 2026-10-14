"""Pushes usage to a Stripe billing meter. Test mode only, enforced in config.

Billing model: the meter sums *completion-equivalent tokens*, and the attached
price divides by 1000. Prompt tokens are folded in at their relative rate so a
single meter can carry a two-rate card -- otherwise you need two meters and two
prices, which is worth doing in production but obscures the mechanics here.

Sends are fire-and-forget on a background task: a billing hiccup must never add
latency to, or fail, an inference request. Every attempt is reconcilable
afterwards against the SQLite ledger.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

log = logging.getLogger("tokenrate.stripe")


class StripeMeter:
    def __init__(self, api_key: str, event_name: str, enabled: bool):
        self.enabled = enabled
        self.event_name = event_name
        self._client = None
        if not enabled:
            return
        import stripe

        stripe.api_key = api_key
        self._stripe = stripe

    async def send(
        self, customer_id: str, billable_units: int, idempotency_key: str | None = None
    ) -> str | None:
        """Returns the meter event identifier, or None if disabled/failed."""
        if not self.enabled or billable_units <= 0 or not customer_id:
            return None

        identifier = idempotency_key or str(uuid.uuid4())

        def _call():
            # Stripe's meter-event payload values must all be strings.
            return self._stripe.billing.MeterEvent.create(
                event_name=self.event_name,
                identifier=identifier,
                payload={
                    "stripe_customer_id": customer_id,
                    "value": str(int(billable_units)),
                },
            )

        try:
            await asyncio.to_thread(_call)
            return identifier
        except Exception as exc:  # noqa: BLE001 - billing must never break serving
            log.warning("stripe meter event failed (%s): %s", identifier, exc)
            return None


def billable_units(
    prompt_tokens: int,
    completion_tokens: int,
    prompt_rate: float,
    completion_rate: float,
) -> int:
    """Collapse a two-rate card onto one meter.

    Prompt tokens are discounted to their completion-token equivalent, so
    `units * completion_rate / 1000` reproduces the true market price.
    """
    if completion_rate <= 0:
        return completion_tokens
    weight = prompt_rate / completion_rate
    return round(prompt_tokens * weight + completion_tokens)
