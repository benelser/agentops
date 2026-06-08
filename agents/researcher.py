"""Researcher agent — searches + fetches + summarizes per the orchestrator's plan.

Three tool calls per query in the happy path; one or more may fail via the
injected error rate in fake_llm.py. Every step opens an AgentOps span so the
Jaeger waterfall reads as a real research session.
"""

from __future__ import annotations

import random
from typing import Any

from fake_llm import fake_complete, fetch_url, search, summarize
from instrumentation import (
    flag_hallucination,
    flow_checkpoint,
    init_telemetry,
    llm_call,
    plan_step,
    tool_call,
)

init_telemetry("researcher")

AGENT_NAME = "researcher"


def research(query: str, rng: random.Random | None = None) -> dict[str, Any]:
    """Run a research pass on `query` and return findings."""
    r = rng if rng is not None else random
    with plan_step(AGENT_NAME, intent=f"research: {query[:40]}"):
        flow_checkpoint("researcher.start", {"query": query[:40]})

        # 1. Decide-what-to-search LLM call.
        prompt = f"Plan a search strategy for: {query}"
        with llm_call("fake-gpt-4o-mini", capture_payload=True, prompt=prompt) as llm:
            resp = fake_complete(prompt, rng=r)
            llm["prompt_tokens"] = resp.prompt_tokens
            llm["completion_tokens"] = resp.completion_tokens
            llm["finish_reason"] = resp.finish_reason
            llm["completion"] = resp.completion

        # 2. Search tool.
        results: list[dict[str, Any]] = []
        try:
            with tool_call("search") as t:
                t["args"] = {"query": query}
                results = search(query, rng=r)
                t["response"] = {"hits": len(results)}
        except Exception:
            # Search failure is recoverable for this demo — fall back to empty.
            flow_checkpoint("researcher.search_failed")

        # 3. Fetch top URL, if any.
        body = ""
        if results:
            top_url = results[0]["url"]
            try:
                with tool_call("fetch_url") as t:
                    t["args"] = {"url": top_url}
                    body = fetch_url(top_url, rng=r)
                    t["response"] = {"bytes": len(body)}
            except Exception:
                flow_checkpoint("researcher.fetch_failed")

        # 4. Summarize.
        summary = ""
        if body:
            with tool_call("summarize") as t:
                t["args"] = {"chars": len(body)}
                summary = summarize(body)
                t["response"] = {"summary_chars": len(summary)}

        # 5. Quality-check LLM pass — and the hallucination injection point.
        prompt = f"Research findings for '{query}': {summary or 'no body'}"
        with llm_call("fake-gpt-4o-mini") as llm:
            resp = fake_complete(prompt, rng=r)
            llm["prompt_tokens"] = resp.prompt_tokens
            llm["completion_tokens"] = resp.completion_tokens
            llm["finish_reason"] = resp.finish_reason
            llm["completion"] = resp.completion

        # 5% chance: the agent "cites" data it doesn't actually have — the
        # exact pattern arXiv 2411.05285 flags as the canonical hallucination
        # case. This is the *explicit* grounding-failure signal.
        if r.random() < 0.05:
            flag_hallucination(
                reason="cited fact absent from retrieved sources",
                evidence=f"query='{query[:60]}' summary_present={bool(summary)}",
                agent=AGENT_NAME,
            )

        flow_checkpoint("researcher.end", {"summary_chars": len(summary)})
        return {
            "query": query,
            "result_count": len(results),
            "summary": summary,
            "model_response": resp.completion,
        }
