"""The token bill for the turbine triage step, with the working shown. Lab 5 Task 9.

    python scripts/llm_cost.py

Offline: every number is computed from recorded fixtures (whose token counts were copied
from Vertex's usage_metadata) and src/llmcost.py. Prices come from scripts/llm_prices.py.
Prints the three figures the lab asks for, plus the Vertex spend of this whole exercise.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import llm_prices  # noqa: E402
from scripts.llm_eval import read_jsonl  # noqa: E402
from src.llmcost import (CACHE_READ_FACTOR, CACHE_WRITE_FACTOR, Usage,  # noqa: E402
                         cache_breakeven_hit_rate, cost_per_1k_requests, output_cap_saving,
                         token_rates)

FIX = ROOT / "evals" / "fixtures"
P = llm_prices.PROVIDER
FLASH, LITE = "gemini-2.5-flash", "gemini-2.5-flash-lite"


def load(name: str) -> list[dict]:
    return read_jsonl(FIX / f"turbine-{name}.jsonl")


def avg_usage(rows: list[dict]) -> tuple[Usage, float, float]:
    n = len(rows)
    tin = sum(r["input_tokens"] for r in rows)
    tout = sum(r["output_tokens"] for r in rows)
    tc = sum(r.get("cached_input_tokens", 0) for r in rows)
    return Usage(round(tin / n), round(tout / n), round(tc / n)), tin / n, tout / n


def line(label: str, value: str) -> None:
    print(f"  {label:<44} {value}")


def main() -> int:
    r_in, r_out = token_rates(P, FLASH)
    print(f"Rates (THB per 1M tokens, {llm_prices.THB_PER_USD} THB/USD, checked 2026-10-16)")
    for m in (FLASH, LITE):
        i, o = token_rates(P, m)
        line(m, f"input {i:.2f}   output {o:.2f}")

    # 1 ---------------------------------------------------------------------------
    base = load("v1-flash")
    u, fin, fout = avg_usage(base)
    print(f"\n1. THB per 1,000 requests — baseline v1 prompt, {FLASH}, thinking off, "
          f"{len(base)} recorded calls")
    line("measured mean input tokens", f"{fin:.1f}  -> {u.input_tokens}")
    line("measured mean output tokens", f"{fout:.1f}  -> {u.output_tokens}")
    line("input  cost per request", f"{u.input_tokens} x {r_in:.2f} / 1e6 = "
         f"{u.input_tokens * r_in / 1e6:.6f} THB")
    line("output cost per request", f"{u.output_tokens} x {r_out:.2f} / 1e6 = "
         f"{u.output_tokens * r_out / 1e6:.6f} THB")
    c_base = cost_per_1k_requests(P, FLASH, u)
    line("x 1,000 requests", f"= {c_base:.2f} THB per 1,000 requests")
    print("  for comparison, same golden set and prompt:")
    for name, model, label in [("v1-flashlite", LITE, "Flash-Lite (gate FAILED, 2 regressions)"),
                               ("v1-flash-thinking", FLASH, "Flash, thinking left at default"),
                               ("v0-flash", FLASH, "Flash, first-draft prompt v0")]:
        uu, _, _ = avg_usage(load(name))
        line(label, f"{uu.input_tokens} in / {uu.output_tokens} out -> "
             f"{cost_per_1k_requests(P, model, uu):.2f} THB per 1,000")

    # 2 ---------------------------------------------------------------------------
    print("\n2. What capping max_output_tokens saves")
    outs = sorted(r["output_tokens"] for r in base)
    line("baseline output tokens, min / median / max", f"{outs[0]} / {outs[len(outs)//2]} / {outs[-1]}")
    for cap in (64, 96):
        uc, _, _ = avg_usage(load(f"v1-flash-cap{cap}"))
        trunc = sum(1 for r in load(f"v1-flash-cap{cap}") if r["finish_reason"] == "MAX_TOKENS")
        line(f"cap {cap}: measured", f"{uc.output_tokens} out avg, {trunc} truncated, "
             f"{cost_per_1k_requests(P, FLASH, uc):.2f} THB per 1,000")
    line("output_cap_saving(baseline mean 47, cap 96)",
         f"{output_cap_saving(P, FLASH, u, 96):.2f} THB per 1,000  (cap above the mean)")
    worst = Usage(u.input_tokens, 1024)
    s_worst = output_cap_saving(P, FLASH, worst, 96)
    line("worst case before: every answer hits 1024", f"{cost_per_1k_requests(P, FLASH, worst):.2f} THB per 1,000")
    line("output_cap_saving(worst case 1024, cap 96)",
         f"(1024 - 96) x {r_out:.2f} / 1e6 x 1000 = {s_worst:.2f} THB per 1,000")
    v0 = load("v0-flash")
    v0_out = round(sum(r["output_tokens"] for r in v0) / len(v0))
    s_len = output_cap_saving(P, FLASH, Usage(u.input_tokens, v0_out), u.output_tokens)
    line(f"length control vs first draft ({v0_out} -> {u.output_tokens} out)",
         f"({v0_out} - {u.output_tokens}) x {r_out:.2f} / 1e6 x 1000 = {s_len:.2f} THB per 1,000")
    th, _, _ = avg_usage(load("v1-flash-thinking"))
    s_think = cost_per_1k_requests(P, FLASH, th) - c_base
    line(f"thinking_budget=0 vs default ({th.output_tokens} -> {u.output_tokens} out)",
         f"{s_think:.2f} THB per 1,000 saved, same 12/13 pass")

    # 3 ---------------------------------------------------------------------------
    prefix = json.loads((FIX / "turbine-v1-prefix-tokens.json").read_text())["system_prompt_tokens"]
    every = [r for f in FIX.glob("turbine-*.jsonl") for r in read_jsonl(f)]
    t_in = sum(r["input_tokens"] for r in every)
    t_cached = sum(r.get("cached_input_tokens", 0) for r in every)
    be = cache_breakeven_hit_rate(P, FLASH, prefix)
    print("\n3. Prompt-cache break-even vs measured")
    line("cacheable prefix (system prompt, count_tokens)", f"{prefix} tokens of {u.input_tokens} input")
    line("llmcost model: write x, read x", f"{CACHE_WRITE_FACTOR} x, {CACHE_READ_FACTOR} x input rate")
    line("break-even = (W-1) / ((1-R) + (W-1))",
         f"({CACHE_WRITE_FACTOR}-1) / ((1-{CACHE_READ_FACTOR}) + ({CACHE_WRITE_FACTOR}-1)) = {be:.1%}")
    line("measured hit rate (all recorded calls)",
         f"{t_cached} cached / {t_in} input tokens over {len(every)} calls = "
         f"{t_cached / t_in:.1%}")
    full_hit = Usage(u.input_tokens, u.output_tokens, prefix)
    line("if every call hit (ceiling of the lever)",
         f"{cost_per_1k_requests(P, FLASH, full_hit):.2f} THB per 1,000 "
         f"(saves {c_base - cost_per_1k_requests(P, FLASH, full_hit):.2f})")
    storage_thb_h = prefix * 1.00 * llm_prices.THB_PER_USD / 1e6
    hit_saving = prefix * r_in * (1 - CACHE_READ_FACTOR) / 1e6
    line("Vertex explicit cache: storage per hour",
         f"{prefix} x 1.00 USD/1M/h x {llm_prices.THB_PER_USD} = {storage_thb_h:.6f} THB/h")
    line("  saving per hit", f"{prefix} x {r_in:.2f} x 0.9 / 1e6 = {hit_saving:.6f} THB")
    line("  break-even traffic", f"{storage_thb_h / hit_saving:.1f} requests per hour")

    # spend -----------------------------------------------------------------------
    print("\nVertex spend for this lab (every recorded call, at the rates above)")
    total = 0.0
    for f in sorted(FIX.glob("turbine-*.meta.json")):
        meta = json.loads(f.read_text())
        i, o = token_rates(P, meta["params"]["model"])
        thb = (meta["input_tokens"] * i + meta["output_tokens"] * o) / 1e6
        total += thb
        line(f.name.replace(".meta.json", ""), f"{meta['calls']} calls, {meta['input_tokens']} in / "
             f"{meta['output_tokens']} out = {thb:.4f} THB")
    line("plus: 1 live attack call, 6 probe calls", "< 0.01 THB")
    line("TOTAL", f"{total:.4f} THB (~{total / llm_prices.THB_PER_USD * 100:.1f} US cents)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
