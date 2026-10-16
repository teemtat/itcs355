"""Lab 5 Task 4 — prove the portability seam is real.

    python scripts/portability_swap_check.py --second-provider azure

Exercises upload, download, and invoke against a SECOND provider's adapter. You are not
migrating the whole system; you are proving the seam exists.

If this fails while `make portability-audit` passes, you have found something worth
writing about: the leak was in configuration or in an assumption, not in an import.

The second provider's slots live in cloud.env beside the primary's, prefixed with the
provider's name (AZURE_BLOB_URI, AZURE_PROJECT_ID, ...), so the swap does not require
editing the primary's slots that the rest of the course reads. A slot with no prefixed
value is inherited from the primary and printed as such: that inherited slot is the
config leak this lab's "Common failure modes" table warns about.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloudlayer.factory import get_adapter
from src import config

# Slot -> Config field. Only the slots an adapter reads for these three methods matter,
# but all are shown so an inherited one is visible.
FIELDS = {
    "PROJECT_ID": "project_id",
    "REGION": "region",
    "BLOB_URI": "blob_uri",
    "CONTAINER_REGISTRY": "container_registry",
    "IDENTITY_REF": "identity_ref",
}
NEEDED = ("PROJECT_ID", "BLOB_URI")   # upload/download read BLOB_URI, invoke PROJECT_ID

# One plausible reading inside service/schemas.py's bounds. An empty batch is a 422,
# which proves only that the validator works.
PROBE_ROW = {"temp_c": 72.0, "vibration_mm_s": 4.1, "pressure_kpa": 210.0,
             "hours_since_service": 900.0, "load_pct": 65.0, "ambient_humidity": 48.0}


def swapped_config(primary: config.Config, provider: str) -> tuple[config.Config, list[str]]:
    prefix = provider.upper() + "_"
    values, inherited = {}, []
    for slot, field in FIELDS.items():
        own = os.environ.get(prefix + slot)
        if own:
            values[field] = own
        else:
            inherited.append(slot)
    return config.Config(**{**primary.__dict__, **values, "provider": provider}), inherited


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--second-provider", required=True, choices=["aws", "azure", "gcp"])
    ap.add_argument("--endpoint", default=os.environ.get("SERVICE", "itcs355-serve"),
                    help="service name on the second provider")
    args = ap.parse_args()

    primary = config.load(strict=False)
    if primary.provider == args.second_provider:
        print("The second provider must differ from CLOUD_PROVIDER.")
        return 1

    swapped, inherited = swapped_config(primary, args.second_provider)
    print(f"primary   {primary.provider:<6} {primary.blob_uri}")
    print(f"secondary {args.second_provider:<6} {swapped.blob_uri}")
    for slot in inherited:
        print(f"  ! {slot} inherited from {primary.provider} "
              f"(set {args.second_provider.upper()}_{slot} in cloud.env)")
    missing = [s for s in NEEDED if s in inherited]
    if missing:
        print(f"\nRefusing to run: {', '.join(missing)} would point the {args.second_provider} "
              f"adapter at {primary.provider}'s resources.")
        return 1
    print()

    adapter = get_adapter(swapped)
    results: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.txt"
        probe.write_text("itcs355 portability probe\n")
        stored: dict[str, str] = {}

        def upload():
            stored["uri"] = adapter.upload(str(probe), "portability/probe.txt")
            return stored["uri"]

        def download():
            back = Path(tmp) / "back.txt"
            adapter.download(stored.get("uri") or f"{swapped.blob_uri}/portability/probe.txt",
                             str(back))
            if back.read_bytes() != probe.read_bytes():
                raise AssertionError("downloaded bytes differ from uploaded bytes")
            return f"{back.stat().st_size} bytes, identical"

        def invoke():
            out = adapter.invoke(args.endpoint, {"rows": [PROBE_ROW]})
            body = out.get("body") or {}
            if out.get("status") != 200 or not body.get("probabilities"):
                raise AssertionError(f"status {out.get('status')}: {str(body)[:160]}")
            return (f"p={body['probabilities'][0]:.3f} model_version={out['model_version']} "
                    f"{out['latency_ms']:.0f} ms")

        for name, call in (("upload", upload), ("download", download), ("invoke", invoke)):
            try:
                results[name] = f"PASS — {call()}"
            except NotImplementedError:
                results[name] = "NOT IMPLEMENTED"
            except Exception as exc:
                results[name] = f"FAIL — {type(exc).__name__}: {exc}"

    for name, outcome in results.items():
        print(f"  [{outcome.split(' —')[0]:<16}] {name:<9} {outcome.partition('— ')[2]}")

    print("\nWrite-up (Lab 5 Task 4), half a page:")
    print("  - which method was hardest to port, and why")
    print("  - one place the abstraction genuinely leaked and could not be hidden")
    print("  - what a full migration would cost in engineering days")
    print("  - was building this abstraction worth it? A well-argued 'no' scores full marks.")

    return 0 if all(v.startswith("PASS") for v in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
