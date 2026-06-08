"""Deterministic fake LLM — hermetic, free, realistic-shaped output.

The fleet must produce dashboards that look like a real agent workload without
calling OpenAI. The lookup table below maps substrings to canned responses;
token counts are sampled from realistic ranges so cost dashboards have signal.

A small probability of failure modes (empty response, timeout) is injected so
the failure-counter panels never sit at zero.
"""

from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class FakeLLMResponse:
    completion: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str  # "stop" | "length" | "empty" | "error"
    model: str
    latency_s: float


# Pattern -> canned completion. Order matters — the first substring match wins.
_PATTERNS: list[tuple[str, str]] = [
    ("decompose", "Step 1: search corpus. Step 2: fetch top result. Step 3: summarize."),
    ("plan", "Plan: research -> synthesize -> verify."),
    ("research", "Findings: three relevant sources identified. Lead source has direct quote."),
    ("synthesize", "Synthesis: the three sources converge on a single answer with one caveat."),
    ("verify", "Verified against source 2. No contradictions found."),
    ("summarize", "Summary: the document argues the central claim with two supporting examples."),
    ("answer", "Answer: based on the gathered evidence, the response is supported."),
    ("compare", "Comparison: option A wins on latency; option B wins on cost."),
    ("explain", "Explanation: the mechanism involves three stages and one feedback loop."),
    ("default", "Acknowledged. Proceeding with the requested action."),
]


def _pick_response(prompt: str) -> str:
    lower = prompt.lower()
    for needle, response in _PATTERNS:
        if needle in lower:
            return response
    return _PATTERNS[-1][1]


def _stable_tokens(text: str, low: int, high: int) -> int:
    """Hash-derived token count so repeats are reproducible."""
    h = int(hashlib.md5(text.encode()).hexdigest(), 16)
    return low + (h % (high - low + 1))


# Failure-injection knobs. Tuned so the dashboards always have *some* failures
# without making the system look broken. With 6 LLM calls per workload tick
# (orchestrator decompose + 2x researcher + 2x synthesizer + verify) at one
# tick every ~3-5s, a 3% empty-response rate yields ~1 empty/min and a 1.5%
# error rate yields ~1 timeout/min. See README friction notes.
EMPTY_RESPONSE_RATE = 0.03
TIMEOUT_RATE = 0.015
SLOW_RESPONSE_RATE = 0.05


def fake_complete(
    prompt: str,
    model: str = "fake-gpt-4o-mini",
    rng: random.Random | None = None,
) -> FakeLLMResponse:
    """Return a deterministic-but-realistic completion for `prompt`."""
    r = rng if rng is not None else random
    # Prompt tokens: 200-2000 range, hash-stable.
    prompt_tokens = _stable_tokens(prompt, 200, 2000)

    # Simulated timeout — raise, caller's llm_call context records error.
    if r.random() < TIMEOUT_RATE:
        time.sleep(0.2)
        raise TimeoutError(f"upstream LLM timed out after 0.2s (simulated)")

    # Empty response — finish_reason=stop with zero completion_tokens.
    if r.random() < EMPTY_RESPONSE_RATE:
        latency = r.uniform(0.05, 0.15)
        time.sleep(latency)
        return FakeLLMResponse(
            completion="",
            prompt_tokens=prompt_tokens,
            completion_tokens=0,
            finish_reason="stop",
            model=model,
            latency_s=latency,
        )

    completion = _pick_response(prompt)
    completion_tokens = _stable_tokens(completion, 50, 500)
    # Slow tail — gives latency histograms a real p99.
    if r.random() < SLOW_RESPONSE_RATE:
        latency = r.uniform(1.5, 3.0)
    else:
        latency = r.uniform(0.05, 0.4)
    time.sleep(latency)

    return FakeLLMResponse(
        completion=completion,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        finish_reason="stop",
        model=model,
        latency_s=latency,
    )


# ---------------------------------------------------------------------------
# Fake tools — deterministic responses, occasional simulated failures.
# ---------------------------------------------------------------------------

TOOL_FAILURE_RATE = 0.04


def search(query: str, rng: random.Random | None = None) -> list[dict[str, Any]]:
    r = rng if rng is not None else random
    if r.random() < TOOL_FAILURE_RATE:
        raise TimeoutError(f"search index unreachable (simulated): {query[:40]}")
    time.sleep(r.uniform(0.02, 0.1))
    seed = int(hashlib.md5(query.encode()).hexdigest(), 16)
    return [
        {"url": f"https://example.com/result/{(seed + i) % 1000}", "snippet": f"Hit {i} for {query[:30]}"}
        for i in range(3)
    ]


def fetch_url(url: str, rng: random.Random | None = None) -> str:
    r = rng if rng is not None else random
    if r.random() < TOOL_FAILURE_RATE:
        raise ConnectionError(f"fetch failed (simulated): {url}")
    time.sleep(r.uniform(0.05, 0.2))
    return (
        f"Document at {url}: this is a canned body of text whose first paragraph "
        f"discusses the topic and whose second paragraph presents supporting "
        f"evidence with citations to other works in the field."
    )


def summarize(text: str) -> str:
    """First 80 chars — deterministic, no failures."""
    return text[:80]
