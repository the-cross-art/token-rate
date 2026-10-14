"""Loads the model registry and rate card, resolving env-var indirections."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ModelRoute:
    alias: str
    backend: str
    base_url: str
    upstream_model: str
    rate_key: str
    speculative: bool
    draft_model: str | None = None
    notes: str = ""


@dataclass
class Settings:
    profile_name: str = ""
    routes: dict[str, ModelRoute] = field(default_factory=dict)
    market_rates: dict[str, dict[str, float]] = field(default_factory=dict)
    local_cost: dict[str, float] = field(default_factory=dict)
    ledger_db: Path = ROOT / "data" / "ledger.db"
    stripe_enabled: bool = False
    stripe_api_key: str = ""
    stripe_event_name: str = "llm_tokens"

    @property
    def cost_per_second(self) -> float:
        """Amortized hardware + electricity, in USD per wall-clock second."""
        lc = self.local_cost
        hardware_per_hour = lc["machine_cost_usd"] / lc["amortization_hours"]
        # Only the delta above idle is attributable to inference.
        marginal_watts = lc["load_watts"] - lc["idle_watts"]
        power_per_hour = (marginal_watts / 1000.0) * lc["electricity_usd_per_kwh"]
        return (hardware_per_hour + power_per_hour) / 3600.0


def _load_profile() -> tuple[str, dict]:
    """The active hardware profile supplies the local cost basis.

    Machine price and power draw are the two inputs that make local cost mean
    anything, and they are per-machine. Keeping them in profiles.yaml rather
    than rates.yaml is what lets the same repo produce correct numbers on a
    16GB Air and a 48GB M5 Pro without edits.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    import profile as _profile  # noqa: PLC0415

    return _profile.resolve()


def load_settings() -> Settings:
    models_cfg = yaml.safe_load((ROOT / "config" / "models.yaml").read_text())
    rates_cfg = yaml.safe_load((ROOT / "config" / "rates.yaml").read_text())
    profile_name, profile = _load_profile()

    backends = models_cfg["backends"]
    routes: dict[str, ModelRoute] = {}
    for alias, spec in models_cfg["models"].items():
        backend_name = spec["backend"]
        backend = backends[backend_name]
        base_url = os.environ.get(backend["base_url_env"], "").rstrip("/")
        if not base_url:
            raise RuntimeError(
                f"{backend['base_url_env']} is unset; model alias '{alias}' "
                f"cannot be routed. Copy .env.example to .env."
            )
        routes[alias] = ModelRoute(
            alias=alias,
            backend=backend_name,
            base_url=base_url,
            upstream_model=spec["upstream_model"],
            rate_key=spec.get("rate_key", alias),
            speculative=bool(spec.get("speculative", False)),
            draft_model=spec.get("draft_model"),
            notes=spec.get("notes", ""),
        )

    stripe_key = os.environ.get("STRIPE_API_KEY", "")
    stripe_on = os.environ.get("STRIPE_ENABLED", "false").lower() == "true"
    if stripe_on and not stripe_key.startswith("sk_test_"):
        raise RuntimeError(
            "Refusing to start: STRIPE_ENABLED=true but STRIPE_API_KEY is not a "
            "test-mode key (sk_test_...). This project never touches live mode."
        )

    return Settings(
        profile_name=profile_name,
        routes=routes,
        market_rates=rates_cfg["market"],
        local_cost=profile["cost"],
        ledger_db=ROOT / os.environ.get("LEDGER_DB", "data/ledger.db"),
        stripe_enabled=stripe_on,
        stripe_api_key=stripe_key,
        stripe_event_name=os.environ.get("STRIPE_METER_EVENT_NAME", "llm_tokens"),
    )
