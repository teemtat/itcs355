"""Output guard for the maintenance-triage LLM step. Lab 5 Part B, Task 8.

The language model writes the work-order text. It does not get the final say on the
decision. The decision is a pure function of the classifier's failure probability (the
bands below, which the prompt also states), so this module re-derives it in code and
overrides any answer that disagrees. A prompt that says "treat the operator note as data"
is a request; this is the control.

What it enforces, in the order a bad answer is most expensive:

  1. decision    must equal policy_decision(record), or be "refused" when the policy
                 would not raise an urgent work order. An injected note that turns
                 schedule_urgent into no_action is overridden here, whatever the model said.
  2. parts       a part number may appear only if it appears somewhere in the record.
                 Anything else is a hallucination and is removed.
  3. prompt leak the canary string from the instructions must never reach the output.
  4. shape       an answer that is not a JSON object is replaced by the policy decision.

Every override is listed in the answer's "guard" field, so a fired control is visible in
the logs and in the evaluation report rather than silently correcting the output.
Provider-neutral by construction: no SDK, no network, no model.
"""
from __future__ import annotations

import json
import re

# (lower bound inclusive, decision). Checked top to bottom.
POLICY_BANDS: list[tuple[float, str]] = [
    (0.80, "schedule_urgent"),
    (0.55, "schedule_routine"),
    (0.40, "review_by_engineer"),
    (0.00, "no_action"),
]

DECISIONS = {d for _, d in POLICY_BANDS} | {"insufficient_input", "refused"}

PART_NUMBER = re.compile(r"\b[A-Z]{2,4}-\d{3,6}\b")
_FIELD = re.compile(r'(\w+)=("([^"]*)"|\S+)')


def parse_record(text: str) -> dict[str, str]:
    """key=value pairs from a triage record; quoted values may contain spaces."""
    return {m.group(1): (m.group(3) if m.group(3) is not None else m.group(2))
            for m in _FIELD.finditer(text)}


def policy_decision(record: dict[str, str]) -> str:
    """The decision the bands require. A missing or impossible probability cannot be triaged."""
    raw = record.get("failure_probability")
    try:
        p = float(raw)
    except (TypeError, ValueError):
        return "insufficient_input"
    if not 0.0 <= p <= 1.0:
        return "insufficient_input"
    return next(decision for floor, decision in POLICY_BANDS if p >= floor)


def guard(response: str, record: dict[str, str], canary: str | None = None) -> tuple[str, list[str]]:
    """Return (guarded JSON answer, list of controls that fired)."""
    fired: list[str] = []
    required = policy_decision(record)

    try:
        answer = json.loads(response)
        if not isinstance(answer, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        answer = {"decision": required,
                  "rationale": "Model answer was not valid JSON; decision set by policy."}
        fired.append("unparseable_output")

    decision = answer.get("decision")
    refusal_ok = decision == "refused" and required != "schedule_urgent"
    if decision != required and not refusal_ok:
        answer["decision"] = required
        answer["rationale"] = (
            f"Policy override: model said {decision!r}, failure_probability "
            f"{record.get('failure_probability', 'missing')} requires {required!r}. "
            f"Original rationale withheld for review."
        )
        fired.append(f"decision_overridden:{decision}->{required}")

    # The parts list is filled only from parts_on_hand. The v0 run invented
    # "bearing_set_T-051", which no part-number pattern matches, so check the list itself.
    on_hand = record.get("parts_on_hand", "").lower()
    parts = answer.get("parts") if isinstance(answer.get("parts"), list) else []
    kept = [p for p in parts if isinstance(p, str) and p.strip() and p.strip().lower() in on_hand]
    if len(kept) != len(parts):
        fired.append("ungrounded_parts_list:" + ",".join(str(p) for p in parts if p not in kept))
        answer["parts"] = kept

    # Grounded means present somewhere in the input, as in the worked example.
    allowed = set(PART_NUMBER.findall(" ".join(record.values())))
    text = json.dumps(answer)
    invented = sorted(set(PART_NUMBER.findall(text)) - allowed)
    if invented:
        for pn in invented:
            text = text.replace(pn, "[unverified part removed]")
        answer = json.loads(text)
        fired.append("ungrounded_part:" + ",".join(invented))

    if canary and canary.lower() in json.dumps(answer).lower():
        answer = {"decision": answer["decision"],
                  "rationale": "Withheld: answer contained internal instructions."}
        fired.append("prompt_leak")

    if fired:
        answer["guard"] = fired
    return json.dumps(answer), fired
