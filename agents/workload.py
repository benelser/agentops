"""Continuous workload driver.

Runs a query through the orchestrator every WORKLOAD_INTERVAL_SECONDS so the
dashboards always have fresh data. Exposes a /health endpoint on :8080 for
the docker healthcheck. Exits cleanly on SIGTERM.
"""

from __future__ import annotations

import logging
import os
import random
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from orchestrator import handle_query

logger = logging.getLogger("agentops.workload")

QUERIES = [
    "What are the trade-offs between vector and graph databases for agent memory?",
    "Compare OpenTelemetry collectors for high-throughput LLM tracing.",
    "Summarize the AgentOps taxonomy from arXiv 2411.05285.",
    "How does flow discovery work for multi-agent systems?",
    "Explain the five-perspective LLM-agent evaluation taxonomy.",
    "What is the failure mode when a tool times out mid-plan?",
    "How should a synthesizer detect contradiction in researcher output?",
    "Compare Jaeger and Tempo for storing LLM agent spans.",
    "Plan a benchmark for hallucination-rate measurement.",
    "Synthesize best practices for observability in production agents.",
    "Verify the claim: agent observability reduces incident MTTR by 40%.",
    "Decompose the problem of measuring planning quality at runtime.",
    "What does runtime non-determinism mean for benchmark validity?",
    "Compare prompt-token cost between gpt-4o-mini and claude-3-haiku.",
    "Plan a research strategy for studying LLM tool-use success rates.",
    "Summarize how OTLP trace context propagates across agent calls.",
    "Explain why a Prometheus scrape interval of 5s is too tight for prod.",
    "Compare Loki and Elasticsearch for agent log aggregation.",
    "How does a flow-discovery analyzer derive expected graphs?",
    "Verify a synthesizer answer against the underlying research findings.",
]


_should_stop = threading.Event()


def _install_signal_handlers() -> None:
    def _handler(signum, _frame) -> None:
        logger.info("workload caught signal %s, stopping", signum)
        _should_stop.set()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


class HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args, **_kwargs) -> None:  # silence default access log
        return

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()


def _serve_health() -> None:
    server = HTTPServer(("0.0.0.0", 8080), HealthHandler)
    logger.info("health server listening on :8080")
    while not _should_stop.is_set():
        server.handle_request()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    _install_signal_handlers()
    interval = float(os.environ.get("WORKLOAD_INTERVAL_SECONDS", "3.0"))

    # Health server in a background thread (each request handled inline).
    health_thread = threading.Thread(target=_serve_health, daemon=True)
    health_thread.start()

    # Deterministic but distinct-per-process random stream.
    rng = random.Random(int(time.time()))
    logger.info("workload starting, interval=%.1fs queries=%d", interval, len(QUERIES))

    tick = 0
    while not _should_stop.is_set():
        query = rng.choice(QUERIES)
        tick += 1
        start = time.perf_counter()
        try:
            result = handle_query(query, rng=rng)
            elapsed = time.perf_counter() - start
            logger.info(
                "tick=%d query=%r elapsed=%.2fs final_chars=%d",
                tick,
                query[:40],
                elapsed,
                len(result.get("final_answer", "")),
            )
        except Exception as exc:
            elapsed = time.perf_counter() - start
            logger.warning(
                "tick=%d query=%r FAILED in %.2fs: %s", tick, query[:40], elapsed, exc
            )
        # Wait between ticks; respect stop signal.
        jitter = rng.uniform(-0.5, 1.0)
        _should_stop.wait(max(0.1, interval + jitter))

    logger.info("workload shutdown after %d ticks", tick)
    return 0


if __name__ == "__main__":
    sys.exit(main())
