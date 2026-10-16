"""Vertex AI Gemini token prices, registered into src/llmcost.py's table. Lab 5 Part B.

Kept out of src/ on purpose: model names are provider strings, and src/ must stay
provider-free. src/llmcost.py does the arithmetic; this file supplies the rates.

Checked 2026-10-16 on cloud.google.com/vertex-ai/generative-ai/pricing (standard tier,
text, <=200K context), USD per 1M tokens:
    gemini-2.5-flash       input 0.30   output 2.50 (response and reasoning)   cached input 0.03
    gemini-2.5-flash-lite  input 0.10   output 0.40                            cached input 0.01
Cached input is 10% of the input rate on both, matching llmcost.CACHE_READ_FACTOR.
Converted at 33.5 THB/USD (early October 2026 traded 32.9-33.7).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import llmcost  # noqa: E402

THB_PER_USD = 33.5
USD_PER_M = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
}
PROVIDER = "gcp"


def register() -> None:
    for model, (usd_in, usd_out) in USD_PER_M.items():
        llmcost.TOKEN_PRICE_TABLE[PROVIDER][model] = (usd_in * THB_PER_USD, usd_out * THB_PER_USD)


register()
