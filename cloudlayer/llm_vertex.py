"""Vertex AI (Gemini) client for the LLM triage step. Lab 5 Part B.

This is the provider-specific half of `generate`: it lives in cloudlayer/ so that src/
stays provider-free and the portability audit stays clean. It returns the same shape the
seam in cloudlayer/base.py documents, with token counts read from the response's own
`usage_metadata` — never estimated.

    from cloudlayer.llm_vertex import generate
    row = generate(prompt, {"model": "gemini-2.5-flash", "max_output_tokens": 256},
                   system="...")

Billing note that is easy to miss: Gemini 2.5 "thinking" tokens are billed as output
tokens but are not part of the visible answer. They are reported separately in
`thoughts_token_count`. `output_tokens` below is what you are billed for (candidates +
thoughts); `thinking_budget` defaults to 0 so the triage step does not think at all.
"""
from __future__ import annotations

import os
import time
from typing import Any

# Not every model is served from every region. gemini-2.5-flash is served from
# asia-southeast1 (same region as the rest of the system); gemini-2.5-flash-lite is not,
# on this project, so it goes through the global endpoint. Measured 2026-10-16.
MODEL_LOCATION = {
    "gemini-2.5-flash": "asia-southeast1",
    "gemini-2.5-flash-lite": "global",
}

_clients: dict[str, Any] = {}


def _client(location: str):
    from google import genai  # imported lazily: offline evals never need the SDK

    if location not in _clients:
        project = os.environ.get("PROJECT_ID", "itcs355-6688143")
        _clients[location] = genai.Client(vertexai=True, project=project, location=location)
    return _clients[location]


def generate(prompt: str, params: dict[str, Any], system: str | None = None) -> dict[str, Any]:
    """Call Gemini on Vertex once. Returns the fixture row shape, usage from the provider."""
    from google.genai import types

    model = params.get("model", "gemini-2.5-flash")
    location = params.get("location") or MODEL_LOCATION.get(model, "global")
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=params.get("temperature", 0.0),
        max_output_tokens=params.get("max_output_tokens", 1024),
        response_mime_type="application/json" if params.get("json", True) else None,
        thinking_config=types.ThinkingConfig(thinking_budget=params.get("thinking_budget", 0)),
        seed=params.get("seed", 355),
    )
    start = time.perf_counter()
    resp = _client(location).models.generate_content(model=model, contents=prompt, config=config)
    latency_ms = round((time.perf_counter() - start) * 1000)

    usage = resp.usage_metadata
    candidates = usage.candidates_token_count or 0
    thoughts = usage.thoughts_token_count or 0
    finish = resp.candidates[0].finish_reason if resp.candidates else None
    return {
        "response": resp.text or "",
        "input_tokens": usage.prompt_token_count or 0,
        "output_tokens": candidates + thoughts,
        "thoughts_tokens": thoughts,
        "cached_input_tokens": usage.cached_content_token_count or 0,
        "latency_ms": latency_ms,
        "finish_reason": getattr(finish, "name", str(finish)),
        "model_version": resp.model_version,
        "location": location,
    }


def count_tokens(text: str, model: str = "gemini-2.5-flash") -> int:
    """The provider's own token count for a piece of text (a free call, no generation).

    Used to size the cacheable prefix (the system instruction) for the cache break-even,
    so that number is measured too rather than estimated from words.
    """
    location = MODEL_LOCATION.get(model, "global")
    return _client(location).models.count_tokens(model=model, contents=text).total_tokens
