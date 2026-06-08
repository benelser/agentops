"""Orchestrator agent — decomposes a query, dispatches researcher + synthesizer.

The top-level entry point is `handle_query`. It opens a root plan-step so the
whole trace rolls up to a single trace_id, then calls researcher.research and
synthesizer.synthesize. Each child is its own plan-step under the root, which
gives Jaeger the natural three-tier waterfall.
"""

from __future__ import annotations

import random
from typing import Any

from fake_llm import fake_complete
from instrumentation import (
    flow_checkpoint,
    init_telemetry,
    llm_call,
    plan_step,
)
from researcher import research
from synthesizer import synthesize

init_telemetry("orchestrator")

AGENT_NAME = "orchestrator"


def handle_query(query: str, rng: random.Random | None = None) -> dict[str, Any]:
    r = rng if rng is not None else random
    with plan_step(AGENT_NAME, intent=f"handle_query: {query[:40]}"):
        flow_checkpoint("orchestrator.start", {"query": query[:60]})

        # 1. Decompose with an LLM call.
        prompt = f"Decompose into subtasks: {query}"
        with llm_call("fake-gpt-4o-mini", capture_payload=True, prompt=prompt) as llm:
            resp = fake_complete(prompt, rng=r)
            llm["prompt_tokens"] = resp.prompt_tokens
            llm["completion_tokens"] = resp.completion_tokens
            llm["finish_reason"] = resp.finish_reason
            llm["completion"] = resp.completion

        # 2. Dispatch to researcher.
        flow_checkpoint("orchestrator.dispatch_researcher")
        findings = research(query, rng=r)

        # 3. Dispatch to synthesizer.
        flow_checkpoint("orchestrator.dispatch_synthesizer")
        synthesis = synthesize(query, findings, rng=r)

        # 4. Final answer LLM call.
        prompt = f"Format final answer for user: {synthesis.get('answer', '')}"
        with llm_call("fake-gpt-4o-mini") as llm:
            resp = fake_complete(prompt, rng=r)
            llm["prompt_tokens"] = resp.prompt_tokens
            llm["completion_tokens"] = resp.completion_tokens
            llm["finish_reason"] = resp.finish_reason
            llm["completion"] = resp.completion

        flow_checkpoint("orchestrator.end")
        return {
            "query": query,
            "findings": findings,
            "synthesis": synthesis,
            "final_answer": resp.completion,
        }
