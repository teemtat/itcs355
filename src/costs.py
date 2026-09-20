"""Cost model. Used by Labs 2, 3, and 5.

GCP rates below were verified on 2026-09-20 for asia-southeast1 (Singapore), which is
the region in cloud.env. Two sources, because the headline price and the billed price
are not the same number on a managed training platform:

  * Vertex AI custom training price per machine type, from the Vertex pricing page with
    the region selector set to Singapore — NOT the default Iowa (us-central1), which is
    23% cheaper and is what the original values in this file were closer to.
  * Compute Engine SKU prices from the Cloud Billing Catalog API
    (service 6F81-5844-456A, serviceRegions contains asia-southeast1), which is the
    same catalogue the invoice is generated from.

WHY BOTH. Vertex bills custom training as discounted infrastructure PLUS a management
fee, and the pricing page is explicit that spot changes only the first part:

    "Spot VMs are billed according to Compute Engine Spot VMs pricing. There are
     [Vertex AI] custom training management fees in addition to your infrastructure
     usage."

So the management fee is the part of the bill a spot machine does not save you, and a
flat "spot is 30% of on-demand" is wrong. Derivation for n1-standard-4 (4 vCPU, 15 GiB):

    Compute Engine on-demand   4(0.038999) + 15(0.005226)  = $0.234386 /h
    Vertex custom training                                   $0.269544 /h  (published)
    management fee             0.269544 - 0.234386         = $0.035158 /h
    Compute Engine spot        4(0.009420) + 15(0.001263)  = $0.056625 /h
    spot + management fee      0.056625 + 0.035158         = $0.091783 /h

    effective spot factor      0.091783 / 0.269544         = 0.34, not 0.30

Rates are THB per hour at USD/THB = 33.36 (ECB reference 2026-09-18: 33.355;
exchangerate-api.com 2026-09-20: 33.356). Re-check the rate before quoting a cost in a
report written on a different day.
"""
from __future__ import annotations

USD_THB = 33.36

# THB/hour, on-demand, for the region named in cloud.env.
#   gcp   : verified 2026-09-20 for asia-southeast1, as documented above.
#   aws   : UNVERIFIED starting values. Verify before quoting them anywhere.
#   azure : UNVERIFIED starting values. Verify before quoting them anywhere.
PRICE_TABLE: dict[str, dict[str, float]] = {
    "local": {"local": 0.0},
    "aws": {
        "ml.m5.large": 4.2,
        "ml.m5.xlarge": 8.4,
        "ml.c5.xlarge": 7.3,
        "ml.g4dn.xlarge": 26.0,
    },
    "azure": {
        "Standard_DS3_v2": 8.1,
        "Standard_F4s_v2": 6.9,
        "Standard_NC4as_T4_v3": 24.5,
    },
    "gcp": {
        "n1-standard-4": 8.99,        # $0.269544/h  Vertex custom training, Singapore
        "e2-standard-4": 6.34,        # $0.190132/h
        "n2-standard-4": 9.19,        # $0.275554/h
        "n1-standard-4+t4": 25.2,     # UNVERIFIED — no GPU trial was run for Lab 2
    },
}

# Measured spot rates, infrastructure + undiscounted management fee. An instance that
# appears here is priced from this table rather than from SPOT_FACTOR.
#
# Note what these two rows say: e2-standard-4 is the cheaper machine on demand and the
# DEARER machine on spot, because E2 spot discounts less steeply than N1 spot. "Pick the
# cheap instance" is not a rule you can apply without checking which price you are paying.
SPOT_RATES: dict[str, dict[str, float]] = {
    "gcp": {
        "n1-standard-4": 3.06,        # $0.091783/h — 34% of on-demand
        "e2-standard-4": 4.14,        # $0.124024/h — 65% of on-demand
    },
}

# Fallback for instances nobody has measured. Roughly right across providers for raw
# compute, and too optimistic for any managed service that adds a per-hour fee.
SPOT_FACTOR = 0.30
DEFAULT_UTILISATIONS = (0.05, 0.25, 0.80)


def hourly_rate(provider: str, instance: str, spot: bool = False) -> float:
    provider = provider.lower()
    table = PRICE_TABLE.get(provider)
    if table is None:
        raise KeyError(f"No price table for provider {provider!r}. Add it to src/costs.py.")
    if instance not in table:
        raise KeyError(
            f"No rate for {instance!r} on {provider}. Known: {sorted(table)}. "
            "Add the instance you actually used — do not substitute a similar one silently."
        )
    if not spot:
        return table[instance]
    measured = SPOT_RATES.get(provider, {}).get(instance)
    return measured if measured is not None else table[instance] * SPOT_FACTOR


def cost_per_1k_predictions(
    hourly_thb: float,
    throughput_rps: float,
    utilisation: float,
) -> float:
    """Cost of 1,000 predictions on an always-on endpoint.

    utilisation is the fraction of provisioned capacity you actually use. It is the most
    fragile number in any serving cost estimate, which is why Lab 3 makes you state it
    explicitly and Lab 5 makes you report three of them.
    """
    if not 0 < utilisation <= 1:
        raise ValueError("utilisation must be in (0, 1]")
    if throughput_rps <= 0:
        raise ValueError("throughput_rps must be positive")
    effective_rps = throughput_rps * utilisation
    seconds_per_1k = 1000.0 / effective_rps
    return hourly_thb * (seconds_per_1k / 3600.0)


def batch_breakeven_rps(
    endpoint_hourly_thb: float,
    batch_job_thb: float,
    batch_runs_per_day: int = 1,
) -> float:
    """Request rate below which scheduled batch inference is cheaper than a warm endpoint.

    Lab 3 asks you to compute this for your own service. The answer is usually lower than
    students expect, which is the point.
    """
    endpoint_daily = endpoint_hourly_thb * 24
    batch_daily = batch_job_thb * batch_runs_per_day
    if batch_daily >= endpoint_daily:
        return 0.0
    return (endpoint_daily - batch_daily) / 86400.0
