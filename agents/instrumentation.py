"""AgentOps span helpers — the taxonomy from arXiv 2411.05285.

The point of this module is to give agent code a small, opinionated set of
context managers that emit the *exact* spans an observability stack needs to
answer the five-perspective evaluation questions (arXiv 2503.16416):

  - planning quality      <- plan_step spans
  - tool-use success      <- tool_call spans + their `success` attribute
  - generalization        <- plan_step `intent` attributes
  - robustness            <- flag_hallucination span events + error_type
  - cost-efficiency       <- llm_call token + latency attributes

And the flow-checkpoint helper (arXiv 2503.06745) emits the structured marks a
post-hoc analyzer can use to derive expected agent flows from runtime logs.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from collections.abc import Iterator
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

# ---------------------------------------------------------------------------
# Boot — wire up tracer / meter / logger providers once per process.
# ---------------------------------------------------------------------------

_OTLP_ENDPOINT = os.environ.get(
    "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318"
)


def _build_resource(service_name: str) -> Resource:
    """Resource attributes follow OTel semantic conventions; the collector's
    `resource` processor adds `deployment.environment` on top."""
    return Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "agentops",
            "service.version": "0.1.0",
        }
    )


_INITIALIZED = False


def init_telemetry(service_name: str) -> None:
    """Boot tracer / meter / logger providers wired to the OTLP collector.

    Idempotent — safe to call from every agent module's import path.
    """
    global _INITIALIZED
    if _INITIALIZED:
        return

    resource = _build_resource(service_name)

    # --- traces ----------------------------------------------------------------
    tracer_provider = TracerProvider(resource=resource)
    span_exporter = OTLPSpanExporter(endpoint=f"{_OTLP_ENDPOINT}/v1/traces")
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    # --- metrics ---------------------------------------------------------------
    metric_exporter = OTLPMetricExporter(endpoint=f"{_OTLP_ENDPOINT}/v1/metrics")
    reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=5000)
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)

    # --- logs ------------------------------------------------------------------
    logger_provider = LoggerProvider(resource=resource)
    log_exporter = OTLPLogExporter(endpoint=f"{_OTLP_ENDPOINT}/v1/logs")
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    set_logger_provider(logger_provider)
    handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()])

    _INITIALIZED = True


# ---------------------------------------------------------------------------
# Module-level tracer + meter — used by every helper below.
# ---------------------------------------------------------------------------

_tracer = trace.get_tracer("agentops.instrumentation")
_meter = metrics.get_meter("agentops.instrumentation")

# Counters that the Prometheus dashboards key off. Created up-front so the
# metric stream is non-empty even before the first failure.
_llm_invocations = _meter.create_counter(
    "agentops_llm_invocations_total",
    description="LLM invocations grouped by model + outcome.",
)
_llm_prompt_tokens = _meter.create_counter(
    "agentops_llm_prompt_tokens_total",
    description="Total prompt tokens by model.",
)
_llm_completion_tokens = _meter.create_counter(
    "agentops_llm_completion_tokens_total",
    description="Total completion tokens by model.",
)
_tool_invocations = _meter.create_counter(
    "agentops_tool_invocations_total",
    description="Tool invocations grouped by tool + success/failure.",
)
_hallucination_events = _meter.create_counter(
    "agentops_hallucination_events_total",
    description="Explicit hallucination flags per agent.",
)
_plan_steps = _meter.create_counter(
    "agentops_plan_steps_total",
    description="Plan-step spans grouped by agent + intent.",
)
_flow_checkpoints = _meter.create_counter(
    "agentops_flow_checkpoints_total",
    description="Flow-discovery checkpoints by name (arXiv 2503.06745).",
)
_llm_latency = _meter.create_histogram(
    "agentops_llm_latency_ms",
    description="LLM call latency in milliseconds.",
    unit="ms",
)
_tool_latency = _meter.create_histogram(
    "agentops_tool_latency_ms",
    description="Tool call latency in milliseconds.",
    unit="ms",
)


# ---------------------------------------------------------------------------
# Public API — context managers for each AgentOps taxonomy category.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def plan_step(agent_name: str, intent: str) -> Iterator[trace.Span]:
    """Open a plan-step span carrying the agent's current intent.

    All nested llm_call / tool_call spans inherit it as their parent, so a
    Jaeger waterfall reads as `orchestrator.plan -> researcher.search ->
    synthesizer.compose`.
    """
    with _tracer.start_as_current_span(
        f"plan_step.{agent_name}",
        attributes={
            "agentops.kind": "plan_step",
            "agentops.agent": agent_name,
            "agentops.intent": intent,
        },
    ) as span:
        _plan_steps.add(1, {"agent": agent_name, "intent": intent})
        try:
            yield span
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise


@contextlib.contextmanager
def llm_call(
    model: str,
    capture_payload: bool = False,
    prompt: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Wrap an LLM invocation.

    Yields a *result dict* the caller mutates with `prompt_tokens`,
    `completion_tokens`, `finish_reason`, `completion`. On exit we copy those
    fields onto the span and bump the appropriate counters.
    """
    result: dict[str, Any] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "finish_reason": "stop",
        "completion": "",
    }
    start = time.perf_counter()
    with _tracer.start_as_current_span(
        "llm_call",
        attributes={
            "agentops.kind": "llm_call",
            "llm.model": model,
        },
    ) as span:
        if capture_payload and prompt is not None:
            span.add_event("llm.prompt", attributes={"prompt": prompt[:1000]})
        try:
            yield result
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000.0
            span.set_attribute("llm.latency_ms", latency_ms)
            span.set_attribute("llm.finish_reason", "error")
            _llm_latency.record(latency_ms, {"model": model})
            _llm_invocations.add(1, {"model": model, "outcome": "error"})
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise
        else:
            latency_ms = (time.perf_counter() - start) * 1000.0
            span.set_attribute("llm.latency_ms", latency_ms)
            span.set_attribute("llm.prompt_tokens", result["prompt_tokens"])
            span.set_attribute("llm.completion_tokens", result["completion_tokens"])
            span.set_attribute("llm.finish_reason", result["finish_reason"])
            if capture_payload:
                span.add_event(
                    "llm.completion",
                    attributes={"completion": result["completion"][:1000]},
                )
            outcome = "ok" if result["finish_reason"] != "error" else "error"
            if result["completion_tokens"] == 0 and result["finish_reason"] == "stop":
                outcome = "empty"  # the simulated empty-response failure mode
            _llm_invocations.add(1, {"model": model, "outcome": outcome})
            _llm_prompt_tokens.add(result["prompt_tokens"], {"model": model})
            _llm_completion_tokens.add(result["completion_tokens"], {"model": model})
            _llm_latency.record(latency_ms, {"model": model})


@contextlib.contextmanager
def tool_call(tool_name: str) -> Iterator[dict[str, Any]]:
    """Wrap a tool invocation.

    Yields a result dict the caller fills with `args`, `response`. On exit we
    snapshot it to the span. Exceptions are caught, recorded as `success=false`
    + `error_type`, then re-raised — so the failure shows up in dashboards
    *and* the caller's try/except still triggers.
    """
    result: dict[str, Any] = {"args": {}, "response": None}
    start = time.perf_counter()
    with _tracer.start_as_current_span(
        f"tool_call.{tool_name}",
        attributes={
            "agentops.kind": "tool_call",
            "tool.name": tool_name,
        },
    ) as span:
        try:
            yield result
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000.0
            span.set_attribute("tool.latency_ms", latency_ms)
            span.set_attribute("tool.success", False)
            span.set_attribute("tool.error_type", type(exc).__name__)
            span.set_attribute("tool.args_json", _safe_json(result.get("args", {})))
            _tool_invocations.add(
                1, {"tool": tool_name, "outcome": "error", "error": type(exc).__name__}
            )
            _tool_latency.record(latency_ms, {"tool": tool_name})
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise
        else:
            latency_ms = (time.perf_counter() - start) * 1000.0
            span.set_attribute("tool.latency_ms", latency_ms)
            span.set_attribute("tool.success", True)
            span.set_attribute("tool.args_json", _safe_json(result.get("args", {})))
            span.set_attribute(
                "tool.response_json", _safe_json(result.get("response"))
            )
            _tool_invocations.add(1, {"tool": tool_name, "outcome": "ok"})
            _tool_latency.record(latency_ms, {"tool": tool_name})


def flag_hallucination(reason: str, evidence: str, agent: str = "unknown") -> None:
    """Explicit grounding-failure flag.

    Records a span event on the *current* span and bumps the hallucination
    counter so dashboards can show rate over time. This is the bright-line
    signal arXiv 2411.05285 calls for — the thing that turns "trust me"
    eval-suite results into runtime ground truth.
    """
    span = trace.get_current_span()
    if span is not None and span.is_recording():
        span.add_event(
            "agentops.hallucination",
            attributes={
                "agentops.hallucination.reason": reason,
                "agentops.hallucination.evidence": evidence[:500],
                "agentops.agent": agent,
            },
        )
    _hallucination_events.add(1, {"agent": agent, "reason": reason})
    logging.getLogger("agentops").warning(
        "hallucination flagged agent=%s reason=%s", agent, reason
    )


def flow_checkpoint(name: str, payload: dict[str, Any] | None = None) -> None:
    """Discovered-flow checkpoint (arXiv 2503.06745).

    Marks a named transition in the agent's execution graph. A downstream
    flow-discovery analyzer can group these by trace_id to reconstruct the
    *actual* graph and diff it against an *expected* one.
    """
    span = trace.get_current_span()
    if span is not None and span.is_recording():
        attrs: dict[str, Any] = {"agentops.flow.checkpoint": name}
        if payload:
            attrs["agentops.flow.payload"] = _safe_json(payload)
        span.add_event("agentops.flow.checkpoint", attributes=attrs)
    _flow_checkpoints.add(1, {"checkpoint": name})


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, default=str)[:1000]
    except Exception:
        return str(value)[:1000]
