"""Synthesizer agent — merges researcher findings into a final answer.

Single plan-step with two LLM calls (compose, verify) and one tool call
(summarize). The verify step is where we can flag a contradiction-style
hallucination — when the synthesizer's answer references a claim that wasn't
in the input.
"""

from __future__ import annotations

import random
from typing import Any

from fake_llm import fake_complete, summarize
from instrumentation import (
    flag_hallucination,
    flow_checkpoint,
    init_telemetry,
    llm_call,
    plan_step,
    tool_call,
)

init_telemetry("synthesizer")

AGENT_NAME = "synthesizer"


def synthesize(
    query: str,
    findings: dict[str, Any],
    rng: random.Random | None = None,
) -> dict[str, Any]:
    r = rng if rng is not None else random
    with plan_step(AGENT_NAME, intent=f"synthesize: {query[:40]}"):
        flow_checkpoint("synthesizer.start", {"hits": findings.get("result_count", 0)})

        # 1. Compose.
        prompt = (
            f"Synthesize a one-paragraph answer for '{query}' using these "
            f"findings: {findings.get('summary', '')}"
        )
        with llm_call("fake-gpt-4o-mini", capture_payload=True, prompt=prompt) as llm:
            resp = fake_complete(prompt, rng=r)
            llm["prompt_tokens"] = resp.prompt_tokens
            llm["completion_tokens"] = resp.completion_tokens
            llm["finish_reason"] = resp.finish_reason
            llm["completion"] = resp.completion
        composed = resp.completion

        # 2. Trim with summarize tool.
        with tool_call("summarize") as t:
            t["args"] = {"chars": len(composed)}
            trimmed = summarize(composed)
            t["response"] = {"chars": len(trimmed)}

        # 3. Verify.
        prompt = f"Verify the synthesis '{trimmed}' is supported by '{findings.get('summary', '')}'"
        with llm_call("fake-gpt-4o-mini") as llm:
            resp = fake_complete(prompt, rng=r)
            llm["prompt_tokens"] = resp.prompt_tokens
            llm["completion_tokens"] = resp.completion_tokens
            llm["finish_reason"] = resp.finish_reason
            llm["completion"] = resp.completion

        # Hallucination: 4% chance the synthesizer "remembers" a source the
        # researcher never returned.
        if not findings.get("summary") and r.random() < 0.4:
            flag_hallucination(
                reason="answer cites sources but researcher returned none",
                evidence=f"composed='{composed[:80]}'",
                agent=AGENT_NAME,
            )
        elif r.random() < 0.04:
            flag_hallucination(
                reason="verification claims support absent from input",
                evidence=f"trimmed='{trimmed[:80]}'",
                agent=AGENT_NAME,
            )

        flow_checkpoint("synthesizer.end")
        return {
            "query": query,
            "answer": trimmed,
            "verification": resp.completion,
        }
