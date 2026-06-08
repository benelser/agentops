# AgentOps recording rules & collector pipeline notes

The four dashboards under `grafana/provisioning/dashboards/` reference a handful
of metrics that are **not native** to a default `spanmetrics` pipeline. This
file is the canonical list — every metric named here is a contract between the
dashboards and R13.1's otel-collector / Prometheus config.

If a query in a dashboard returns "No data", check this file first; the rule
or processor may not be wired yet.

---

## Metrics that come "for free" from the spanmetrics processor

These need only a `spanmetricsconnector` (or the legacy `spanmetrics`
processor) in the collector pipeline. Names assume the default
`namespace: agent` and a per-attribute dimension list including
`agent.name`, `llm.model`, `tool.name`, `tool.success`, `tool.error_type`,
`status.code`. Concretely the collector should configure:

```yaml
connectors:
  spanmetrics:
    namespace: agent
    histogram:
      explicit:
        buckets: [10ms, 50ms, 100ms, 250ms, 500ms, 1s, 2.5s, 5s, 10s, 30s]
    dimensions:
      - name: agent.name
        default: unknown
      - name: status.code
        default: ok
```

Resulting metrics referenced by dashboards:

- `agent_request_duration_seconds_bucket{agent,status,le}`
- `agent_request_duration_seconds_count{agent,status}`
- `agent_request_duration_seconds_sum{agent,status}`

**Note for R13.1:** the spanmetricsconnector emits `_milliseconds` units by
default in some versions. The dashboards assume `_seconds`. Either configure
`unit: s` on the connector, or rename my dashboard queries — flag this back to
me if you pick the latter.

---

## Counters R13.1 emits directly (no spanmetrics needed)

The agent code creates these via the OTel Metrics API and exports them
through the same OTLP pipe; the collector forwards to Prometheus.

- `llm_tokens_total{agent,model,kind}` — `kind` is `prompt` or `completion`.
- `tool_calls_total{agent,tool,success,error_type}` — `success` is the
  *string* `"true"` / `"false"` (Prometheus label, not bool). `error_type` is
  empty/absent on success.
- `hallucinations_total{agent,reason}`
- `tool_call_duration_seconds_bucket{agent,tool,le}` — histogram for the
  P95-by-tool panel and the heatmap.

If any of these don't exist in the agent code yet, the relevant panels show
"No data". The agent-fleet contract owner should treat these as the minimum
emit set.

---

## Flow discovery — the non-trivial metrics

The four flow-discovery panels need a notion of **"the sequence of
`flow.checkpoint` values for a given trace"**. There is no built-in OTel
processor that emits this — span attributes are per-span, not per-trace.
Three places we could compute it:

### Option A (chosen): Loki structured logs + LogQL aggregation

Every span carrying a `flow.checkpoint` attribute also writes a structured
log line via the agent's logger — that log line is shipped to Loki with the
fields `{trace_id, agent, checkpoint, ts}`. The flow-discovery dashboard's
"Top-N flows" and "Flow divergence" panels use a LogQL query like:

```logql
sum by (sequence) (
  count_over_time(
    {service_name="agent-fleet"}
      | json
      | __error__=""
      | line_format "{{.trace_id}}|{{.checkpoint}}"
    [1h]
  )
)
```

…and a Grafana **transformation** (`groupBy` → `concat`) reshapes the rows
into one-row-per-trace, sequence string.

Trade-off: Loki transformations cost CPU on the Grafana side; OK for ~1k
traces/h, painful at 100k.

### Option B: Prometheus recording rule on a custom span event counter

R13.1 adds an OTel processor that, for each *root* span, emits a single
metric sample with a synthetic label `sequence` derived from the ordered
checkpoint list. The collector config would need a custom transform
processor — sketched below. The resulting Prometheus series is then
trivially queryable as `topk(10, sum by (sequence)
(rate(flow_sequence_total[1h])))`.

This is cleaner at query time but requires R13.1 to write a transform
processor (not built-in). TODO until the collector grows the capability.

### Option C: Prometheus recording rule on a span event counter

If R13.1 adds a `flow_checkpoint_total{agent,checkpoint}` counter (one
increment per checkpoint event), Prometheus can compute *per-agent mean
checkpoints per trace* via a recording rule, but **cannot** reconstruct the
ordered sequence — the order is lost when buckets are summed.

So the dashboard uses C for the "mean checkpoints per trace, by agent" bar
gauge, and A for the sequence-aware panels.

---

## Concrete recording rules

Add these to `prometheus.yml` under `rule_files:`, then drop the file in
`/etc/prometheus/rules/`. R13.1: please wire this in.

```yaml
groups:
  - name: agentops_flow
    interval: 30s
    rules:
      # Mean checkpoints per trace, per agent. Trace_id is on the span; the
      # collector emits flow_checkpoint_total with attribute agent and
      # exemplar trace_id. We approximate "per trace" as
      # increase(checkpoints) / increase(distinct trace starts) where a
      # trace start is the root span (status=ok or status=error).
      - record: agent:flow_checkpoints_per_trace_1h
        expr: |
          sum by (agent) (increase(flow_checkpoint_total[1h]))
          /
          clamp_min(
            sum by (agent) (increase(agent_request_duration_seconds_count{span_kind="server"}[1h])),
            1
          )

      # Total flow-checkpointed traces in the last hour. Used as the
      # denominator for the stability score.
      - record: agent:flow_traces_1h
        expr: |
          sum(increase(agent_request_duration_seconds_count{span_kind="server"}[1h]))

      # Flow stability score: 1 - (distinct sequences / total traces).
      # The numerator depends on a separate Loki ruler that materializes
      # flow_distinct_sequences_1h from the per-trace checkpoint logs
      # (see Option A in this file). Until the Loki ruler is wired, this
      # rule is a no-op and the dashboard will read NaN.
      - record: agent:flow_stability_score_1h
        expr: |
          1 - (
            flow_distinct_sequences_1h
            /
            clamp_min(agent:flow_traces_1h, 1)
          )

  - name: agentops_cost
    interval: 60s
    rules:
      # Pre-aggregated USD/sec, for the per-second burn-down chart.
      # Rates are baked at recording time using sensible defaults; for the
      # configurable variant the dashboard still computes USD inline.
      - record: agent:llm_cost_usd_per_second
        expr: |
          (sum(rate(llm_tokens_total{kind="prompt"}[5m])) / 1000) * 0.01
          +
          (sum(rate(llm_tokens_total{kind="completion"}[5m])) / 1000) * 0.03
```

---

## Collector pipeline sketch (for R13.1)

The flow-checkpoint counter and the structured log line both need a small
custom processor in the collector. Pseudo-config:

```yaml
processors:
  attributes/checkpoint-counter:
    actions:
      - key: flow.checkpoint
        action: extract
        pattern: ^(?P<checkpoint>.+)$

  # Spanevent-to-metric: emit one flow_checkpoint_total per span that
  # carries a flow.checkpoint attribute. Requires the
  # spanmetricsconnector v0.114+ which supports event-as-metric.
  spanevents:
    enabled: true
    metric_name: flow_checkpoint_total
    dimensions:
      - agent.name
      - flow.checkpoint
```

And the agent code must call `logger.info(...)` with the same structured
fields whenever it sets `flow.checkpoint`, so the Loki side gets the same
data the Prometheus side does.

---

## TODO list for R13.1

- [ ] `spanmetricsconnector` configured with `agent.name` and `status.code`
      dimensions, emitting `agent_request_duration_seconds_*` in seconds.
- [ ] Direct counters: `llm_tokens_total`, `tool_calls_total`,
      `hallucinations_total`, `tool_call_duration_seconds`.
- [ ] `flow_checkpoint_total{agent,checkpoint}` counter via spanevents.
- [ ] Loki ingestion of agent structured logs with `{trace_id, agent,
      checkpoint}` fields.
- [ ] Prometheus `rule_files` block loading the recording rules above.

When all five are checked, every panel in every dashboard renders real data.
