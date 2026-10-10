"""Generate the default dataset for Lab 1.

Deterministic: the same seed always produces a byte-identical file, so the data
fingerprint is stable across machines. Swap this out if you bring your own problem —
the rest of the repo does not care where data/raw/sensors.csv came from.

Machines have persistent characteristics, which is exactly why the split must be
grouped by machine_id. See src/data.split.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

N_MACHINES = 240
READINGS_PER_MACHINE = 25


def _reading(rng: np.random.Generator, base_temp: float, wear: float, duty: float) -> dict:
    """One reading from a machine with these traits. Draws in a fixed order, so the
    default dataset stays byte-identical to the one Lab 1 pinned."""
    hours = float(rng.uniform(0, 9000))
    temp = base_temp + 0.0016 * hours + rng.normal(0, 2.2)
    vib = 1.4 + 0.55 * wear + 0.00035 * hours + rng.normal(0, 0.45)
    pressure = 320 - 0.7 * wear + rng.normal(0, 14)
    load = float(np.clip(100 * duty + rng.normal(0, 6), 0, 100))
    humidity = float(np.clip(rng.normal(58, 12), 0, 100))

    logit = (
        -6.1
        + 0.052 * (temp - 72)
        + 0.71 * (vib - 2.2)
        + 0.00021 * hours
        + 0.019 * (load - 60)
        - 0.004 * (pressure - 320)
    )
    p = 1.0 / (1.0 + np.exp(-logit))
    return {
        # Upstream change: the new sensor gateway reports Fahrenheit, column name unchanged.
        "temp_c": round(float(temp) * 9 / 5 + 32, 3),
        "vibration_mm_s": round(float(vib), 3),
        "pressure_kpa": round(float(pressure), 3),
        "hours_since_service": round(hours, 3),
        "load_pct": round(load, 3),
        "ambient_humidity": round(humidity, 3),
        "failed_within_7d": int(rng.random() < p),
    }


def build(seed: int, reading_seed: int | None = None) -> pd.DataFrame:
    """The fleet comes from `seed`. With `reading_seed`, the same machines produce NEW
    readings: what production looks like after training, used by Lab 4 as the input
    stream. A new `seed` would be a different fleet, which is a different question."""
    rng = np.random.default_rng(seed)
    later = np.random.default_rng(reading_seed) if reading_seed is not None else None
    rows = []
    reading_id = 0
    for machine in range(N_MACHINES):
        # Persistent per-machine traits. A row-wise split would let a model memorise these.
        base_temp = rng.normal(72, 9)
        wear = rng.gamma(2.0, 1.4)
        duty = rng.uniform(0.3, 1.0)
        for _ in range(READINGS_PER_MACHINE):
            # Always draw the original reading: the next machine's traits come from the
            # same generator, so skipping it would quietly change the fleet.
            values = _reading(rng, base_temp, wear, duty)
            if later is not None:
                values = _reading(later, base_temp, wear, duty)
            rows.append({"reading_id": reading_id, "machine_id": machine, **values})
            reading_id += 1
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=20260101)
    ap.add_argument("--out", type=Path, default=Path("data/raw/sensors.csv"))
    ap.add_argument("--reading-seed", type=int, default=None,
                    help="new readings from the same fleet (Lab 4 input stream)")
    args = ap.parse_args()

    df = build(args.seed, args.reading_seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.out, index=False, lineterminator="\n")
    rate = df["failed_within_7d"].mean()
    print(f"wrote {args.out}  rows={len(df)}  machines={df.machine_id.nunique()}  positive_rate={rate:.3f}")


if __name__ == "__main__":
    main()
