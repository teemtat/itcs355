"""Record real model responses for a golden set into a fixture. Lab 5 Part B.

    python scripts/llm_record.py --prompt evals/prompts/turbine-v1.txt \
        --model gemini-2.5-flash --out evals/fixtures/turbine-v1-flash.jsonl

One call per golden case, no retries, no loops: this is the only script in the repo that
spends money on tokens, and it refuses to overwrite a fixture unless told to. Token counts
are copied from the provider's usage fields by cloudlayer/llm_vertex.py. A sidecar
`<fixture>.meta.json` records exactly what produced the fixture (model, region, prompt
hash, parameters, date), because a fixture you cannot regenerate is an anecdote.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cloudlayer.llm_vertex import generate  # noqa: E402
from scripts.llm_eval import read_jsonl, shown  # noqa: E402


def record(golden: list[dict], system: str, params: dict) -> list[dict]:
    rows = []
    for case in golden:
        row = generate(case["prompt"], params, system=system)
        print(f"  {case['id']}  in={row['input_tokens']:>4} out={row['output_tokens']:>4} "
              f"{row['latency_ms']:>5} ms  {row['finish_reason']}")
        rows.append({"id": case["id"], **row})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--golden", type=Path, default=ROOT / "evals" / "golden" / "turbine.jsonl")
    ap.add_argument("--prompt", type=Path, required=True, help="system instruction file")
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-output-tokens", type=int, default=1024)
    ap.add_argument("--thinking-budget", type=int, default=0,
                    help="0 = no thinking (default); -1 = the model's own dynamic default")
    ap.add_argument("--ids", help="comma-separated case ids; default all")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--force", action="store_true", help="overwrite an existing fixture")
    args = ap.parse_args()

    if args.out.exists() and not args.force:
        raise SystemExit(f"{args.out} exists — it is a recorded result, pass --force to spend "
                         "tokens replacing it")

    golden = read_jsonl(args.golden)
    if args.ids:
        wanted = set(args.ids.split(","))
        golden = [c for c in golden if c["id"] in wanted]
    system = args.prompt.read_text()
    params = {"model": args.model, "temperature": args.temperature,
              "max_output_tokens": args.max_output_tokens, "thinking_budget": args.thinking_budget, "json": True}

    print(f"recording {len(golden)} case(s) with {args.model} -> {args.out}")
    rows = record(golden, system, params)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    meta = {
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "golden": shown(args.golden),
        "prompt_file": shown(args.prompt),
        "prompt_sha256": hashlib.sha256(system.encode()).hexdigest()[:16],
        "params": params,
        "location": rows[0]["location"] if rows else None,
        "model_version": rows[0]["model_version"] if rows else None,
        "calls": len(rows),
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "output_tokens": sum(r["output_tokens"] for r in rows),
    }
    args.out.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"wrote {shown(args.out)}  ({meta['input_tokens']} in / "
          f"{meta['output_tokens']} out tokens)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
