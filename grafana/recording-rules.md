# AgentOps recording rules, gaps, and instrumentation TODOs

The four dashboards under `grafana/provisioning/dashboards/` are written
against the metric names **R13.1's agents actually emit today** (see
`agents/instrumentation.py`). This file documents (a) the panels that are
fully live against the current pipeline, (b) the small instrumentation
changes that would close remaining gaps, and (c) the recording rules R13.1
would need to load in `prometheus.yml` to unlock the more advanced
flow-discovery panels.

---

## What's emitted today (agents/instrumentation.py)

```
agentops_llm_invocations_total           {model, outcome}        outcome=ok|error|empty
agentops_llm_prompt_tokens_total         {model}
agentops_llm_completion_tokens_total     {model}
agentops_llm_latency_ms_bucket           {model, le}             unit: ms (histogram)
agentops_tool_invocations_total          {tool, outcome, error}  outcome=ok|error
agentops_tool_latency_ms_bucket          {tool, le}              unit: ms (histogram)
agentops_hallucination_events_total      {agent, reason}
agentops_plan_steps_total                {agent, intent}
agentops_flow_checkpoints_total          {checkpoint}            # NO agent label
```

Resource attributes attached to every metric (via OTel resource processor):

```
service_name="agent-fleet"
service_namespace="agentops"
deployment_environment="demo"
telemetry_source="agentops-collector"
```

Spans carry the agent's name on the `agentops.agent` attribute (plan_step
spans) and via the span name (`tool_call.search`, etc.). The agent name
**is not** propagated onto LLM / tool / flow_checkpoint metrics — see
the gap section.

---

## Gaps the dashboards highlight

Every dashboard description block names the panels that approximate the spec
because of a missing label. The fixes are short:

### Gap 1: `agentops_flow_checkpoints_total` is missing the `agent` label

Dashboard 4 ("Mean checkpoints per trace, by agent") falls back to a
fleet-wide value. Fix:

```python
# instrumentation.py::flow_checkpoint, line 319
_flow_checkpoints.add(1, {"checkpoint": name, "agent": _current_agent()})
```

where `_current_agent()` reads the in-context agent name (a `ContextVar`
set by `plan_step`, or pulled off the active span's `agentops.agent`
attribute).

### Gap 2: LLM token/latency counters are missing the `agent` label

Dashboard 1 P95-by-agent falls back to per-model. Dashboard 2 per-agent token
attribution becomes activity-share via `plan_steps_total`. Fix:

```python
# instrumentation.py::llm_call, lines 232-234
_llm_prompt_tokens.add(result["prompt_tokens"], {"model": model, "agent": _current_agent()})
_llm_completion_tokens.add(result["completion_tokens"], {"model": model, "agent": _current_agent()})
_llm_latency.record(latency_ms, {"model": model, "agent": _current_agent()})
```

### Gap 3: tool counters are missing the `agent` label

Dashboard 1 "failures by agent + error_type" falls back to "failures by tool
+ error_type". Same fix pattern.

### Gap 4: no per-trace structured log line for checkpoints

Dashboard 4's sequence-aware panels (true Top-N flows, true divergence rate)
need each `flow_checkpoint()` call to write a structured log record that
Loki ingests with `{trace_id, agent, checkpoint}` keys. The OTel log handler
is already wired (`instrumentation.py:88-94`), so it's one line:

```python
# instrumentation.py::flow_checkpoint, after line 318
logging.getLogger("agentops.flow").info(
    "checkpoint",
    extra={
        "trace_id": format(span.get_span_context().trace_id, "032x") if span else "",
        "agent": _current_agent(),
        "checkpoint": name,
    },
)
```

Then the recording rule below becomes viable.

---

## Recording rules to load via prometheus.yml

R13.1: add this to `prometheus.yml` once Gaps 1-4 are closed.

```yaml
# prometheus.yml — add at top level
rule_files:
  - /etc/prometheus/rules/*.yml
```

…and mount `./prometheus-rules:/etc/prometheus/rules:ro` in
`docker-compose.yml`. The rule file:

```yaml
# prometheus-rules/agentops.yml
groups:
  - name: agentops_flow
    interval: 30s
    rules:
      # Once flow_checkpoints carries `agent`, this becomes the bar gauge
      # in dashboard 4.
      - record: agent:flow_checkpoints_per_plan_step_1h
        expr: |
          sum by (agent) (increase(agentops_flow_checkpoints_total[1h]))
          /
          clamp_min(sum by (agent) (increase(agentops_plan_steps_total[1h])), 1)

      # Sequence stability score. Depends on a Loki ruler that materializes
      # `flow_distinct_sequences_1h` from per-trace checkpoint logs (gap 4).
      # Until then, dashboard 4's stat falls back to a Prometheus-only proxy
      # written inline in the panel.
      - record: agent:flow_stability_score_1h
        expr: |
          1 - (
            flow_distinct_sequences_1h
            /
            clamp_min(
              sum(increase(agentops_plan_steps_total{intent=~"handle_query.*"}[1h])),
              1
            )
          )

  - name: agentops_cost
    interval: 60s
    rules:
      # Pre-aggregated USD/sec for the burn-down chart. Hard-codes the
      # default rates (0.01/0.03 per 1K). Dashboard 2 still computes spend
      # inline so the rates can be tweaked from the UI.
      - record: agent:llm_cost_usd_per_second_default_rates
        expr: |
          (sum(rate(agentops_llm_prompt_tokens_total[5m])) / 1000) * 0.01
          +
          (sum(rate(agentops_llm_completion_tokens_total[5m])) / 1000) * 0.03
```

---

## Loki ruler — for true per-trace sequence reconstruction (gap 4 fix)

Once Gap 4 lands (structured logs include `trace_id` + `checkpoint`), Loki
can materialize a `flow_distinct_sequences_1h` *Prometheus* metric via its
ruler. That metric is what `agent:flow_stability_score_1h` consumes.

A sketch (Loki recording rule, dropped in Loki's ruler config):

```yaml
# loki-rules/agentops.yml
groups:
  - name: agentops_flow_loki
    interval: 60s
    rules:
      # Number of distinct (trace_id, sequence) tuples in the last hour. The
      # sequence label is constructed by `label_format` from the concatenated
      # ordered checkpoint events per trace_id; Loki's `label_join` /
      # `label_format` aggregators in v3.x give us enough to do this in a
      # single ruler pass.
      - record: flow_distinct_sequences_1h
        expr: |
          count(count by (sequence) (
            sum by (trace_id, sequence) (
              count_over_time(
                {service_name="agent-fleet"}
                  | json
                  | checkpoint != ""
                  | label_format sequence=`{{.checkpoint}}`
                [1h]
              )
            )
          ))
```

Loki's ruler runs the LogQL and writes the result back to Prometheus via the
remote_write endpoint. The cleaner alternative — a custom OTel collector
processor that builds the sequence per root span — is documented below but
not implemented.

---

## Alternative: custom collector processor (sketch only)

If we ever want sequence reconstruction without Loki, the collector grows a
small custom processor:

```yaml
# otel-collector-config.yaml, hypothetical addition
processors:
  span_sequence:
    # For each root span, walk children in temporal order, collect
    # `agentops.flow.checkpoint` event names, emit one metric with
    # label sequence="orchestrator.start|researcher.start|...".
    metric_name: flow_sequence_total
    dimensions:
      - service.name
```

Not built-in to the contrib collector today (as of v0.118.0). Listed as a
future-work item.

---

## TODO checklist for R13.1

- [ ] **Gap 1:** add `agent` label to `agentops_flow_checkpoints_total`.
- [ ] **Gap 2:** add `agent` label to `agentops_llm_{prompt,completion}_tokens_total` and `agentops_llm_latency_ms`.
- [ ] **Gap 3:** add `agent` label to `agentops_tool_invocations_total` and `agentops_tool_latency_ms`.
- [ ] **Gap 4:** emit a structured log line from `flow_checkpoint()` carrying `{trace_id, agent, checkpoint}`.
- [ ] **Recording rules:** add `rule_files:` to `prometheus.yml` and mount the rules file.
- [ ] **Loki ruler:** add the Loki ruler config block so it can write `flow_distinct_sequences_1h` back to Prometheus.

When the first four are checked, every panel in every dashboard shows real
data. Items 5-6 unlock the "production-grade" variants of the flow-stability
score (dashboard 4) and the per-agent cost burn-down (dashboard 2).
