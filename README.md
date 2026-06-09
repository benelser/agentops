# AgentOps

Production-ready observability for LLM agent systems, distilled from three
arXiv papers ([2411.05285][p1], [2503.06745][p2], [2503.16416][p3]).

A complete OpenTelemetry stack — collector, Jaeger, Prometheus, Loki, Grafana
— plus a three-agent Python fleet that emits the *exact* taxonomy of telemetry
the papers argue an agent system needs to be safe in production. Clone it,
`docker compose up`, open Grafana, watch agents work in real time.

## 🎬 Watch the lunch-and-learn

<video src="https://github.com/benelser/agentops/raw/main/media/agentops-lunch-and-learn.mp4" controls poster="https://github.com/benelser/agentops/raw/main/diagrams/lunch-and-learn-thumb.png" width="100%"></video>

> If your client doesn't render the inline player above, fall back to:
> **[Play the MP4 on GitHub](https://github.com/benelser/agentops/blob/main/media/agentops-lunch-and-learn.mp4)** ·
> **[Download the latest cut (v0.4.0)](https://github.com/benelser/agentops/releases/download/v0.4.0/agentops-lunch-and-learn.mp4)** ·
> 1920×1080 · 9:38 · H.264 + AAC · −16 LUFS · 44 MB · OpenAI gpt-4o-mini-tts (`ash`)

A 10:59 narrated walkthrough of the stack — built end-to-end by
[**docent**][docent] from this repo's [runbook](runbook/),
[diagrams](diagrams/), and live recordings of the four dashboards under real
agent traffic. Voiced as a **peer engineer giving a casual lunch-and-learn**,
not a documentary — every beat carries free-text performance direction
routed to `gpt-4o-mini-tts.instructions` so the inflection lands where it
should.

The film opens by killing one misconception:

> "OpenTelemetry on an LLM agent is just HTTP request tracing."

Then it walks the five-span taxonomy from [paper 1][p1], shows a real
Jaeger trace built from those spans, plays the live agent fleet with
cursor-and-pin computer-use overlays guiding the eye, annotates the Python
decorator API, names where the runbook does *not* apply, and closes on the
flow-stability score from [paper 2][p2] that tells you the plan is drifting
while every individual request still looks fine. It ends the way a real
lunch-and-learn ends: **"That's it. Questions?"**

The v0.3 cut is the one where docent's dogfood drove **six concrete
extension points** into existence — two new scene types (`waterfall`,
`query`), the `demonstrate` cursor+pin overlay grammar, a custom preset
that re-themes the scene chrome (not just the colors), a `ChromeTokens`
preset extension, and the first real third-party `FeaturePlugin`
(`agentopsContextHud` — the bottom-left observability HUD with drifting
stability dot). See the [v0.3 release notes][v3notes] for the architecture
deltas.

[v3notes]: https://github.com/benelser/agentops/releases/tag/v0.3.0

**Earlier cuts:**
- [v0.3.0](https://github.com/benelser/agentops/releases/tag/v0.3.0) (6 docent extension points, 10:59, OpenAI ash)
- [v0.2.0](https://github.com/benelser/agentops/releases/tag/v0.2.0) (OpenAI ash, 9:42, conversational tone)
- [v0.1.0](https://github.com/benelser/agentops/releases/tag/v0.1.0) (Kokoro, 7:45, documentary register)

> **This repository was authored end-to-end by [docent][docent] — an
> explanation-film engine that takes a curated knowledge-base directory and
> renders a narrated lunch-and-learn film.** The whole stack you see here,
> from the docker-compose to the 5,806-word operator runbook, was produced
> by parallel-DAG worktree-isolated agents under docent's orchestration.
> See [§ How this was built](#how-this-was-built).

## Quick start

```bash
git clone <this repo>
cd agentops
docker compose up -d
# wait ~30 seconds for all services to come up healthy
open http://localhost:3000   # Grafana, admin/admin
```

You will see four datasources auto-wired (Prometheus, Loki, Jaeger) and live
agent traffic flowing within ~10 seconds of the `agent-fleet` container
starting.

## Quick links

| Service     | URL                       | Why open it                          |
|-------------|---------------------------|--------------------------------------|
| Grafana     | http://localhost:3000     | Dashboards (admin/admin)             |
| Jaeger UI   | http://localhost:16686    | Distributed traces, waterfall view   |
| Prometheus  | http://localhost:9090     | Raw metric queries, scrape targets   |
| Loki        | http://localhost:3100     | Log API (use Grafana for the UI)     |
| Agent fleet | http://localhost:8080/health | Liveness                          |

## Architecture

```
                     +------------------+
                     |  agent-fleet     |
                     |  (Python, OTLP)  |
                     +--------+---------+
                              | OTLP HTTP :4318
                              v
                  +-----------+-----------+
                  |  otel-collector       |
                  |  receivers > batch >  |
                  |  resource > export    |
                  +--+-------+---------+--+
                     |       |         |
              traces |       | metrics | logs
                     v       v         v
              +------+--+ +--+----+ +--+----+
              | jaeger  | | prom  | | loki  |
              | :16686  | | :9090 | | :3100 |
              +----+----+ +---+---+ +---+---+
                   |          |         |
                   +----+-----+----+----+
                        v          v
                    +---+----------+---+
                    |     grafana      |
                    |     :3000        |
                    +------------------+
```

## What the agents do

A three-agent system answers natural-language queries through a plan-step
graph:

1. **orchestrator** decomposes the query, dispatches subtasks, formats the
   final answer.
2. **researcher** runs `search`, `fetch_url`, `summarize` tools and returns
   findings.
3. **synthesizer** composes a one-paragraph answer and verifies it against
   the researcher's findings.

Every step emits AgentOps-taxonomy spans (`plan_step`, `llm_call`,
`tool_call`) and explicit failure signals (`flag_hallucination`,
`flow_checkpoint`). The LLM is a deterministic fake — no API costs, hermetic
operation, same trace shape as production.

### Failure injection

To keep the dashboards interesting, the workload injects realistic failures
at calibrated rates (see `agents/fake_llm.py`):

- `EMPTY_RESPONSE_RATE = 3%` — LLM returns zero completion tokens.
- `TIMEOUT_RATE = 1.5%` — LLM upstream timeout.
- `SLOW_RESPONSE_RATE = 5%` — gives the latency histogram a real p99 tail.
- `TOOL_FAILURE_RATE = 4%` — search/fetch tools raise.
- `~5% per agent per query` — explicit `flag_hallucination` call.

At one workload tick every 3-5 seconds with ~6 LLM calls and ~4 tool calls
per tick, you get roughly **one of each failure mode per minute**. Dashboards
never sit at zero; nothing looks catastrophically broken.

## Tech stack (pinned)

| Component   | Image                                              | Why pinned                 |
|-------------|----------------------------------------------------|----------------------------|
| Collector   | `otel/opentelemetry-collector-contrib:0.118.0`     | Stable OTLP/loki exporter  |
| Jaeger      | `jaegertracing/all-in-one:1.62.0`                  | OTLP receivers GA          |
| Prometheus  | `prom/prometheus:v2.55.1`                          | Last 2.x LTS               |
| Loki        | `grafana/loki:3.2.1`                               | OTLP ingest stable         |
| Grafana     | `grafana/grafana:11.3.0`                           | Tracesv2 + nodeGraph       |
| Python      | `python:3.12-slim` + `uv 0.5.7`                    | Matches docent pattern     |
| OTel SDK    | `1.28.2` (api/sdk/exporter)                        | Wire-compat with collector |

## The papers

- **[arXiv 2411.05285][p1] — *AgentOps: Enabling Observability of LLM
  Agents*.** Proposes a span taxonomy (plan-step, llm-invocation, tool-call,
  hallucination flag) so non-deterministic agent behavior becomes
  inspectable. Embodied in `agents/instrumentation.py`.

- **[arXiv 2503.06745][p2] — *Beyond Black-Box Benchmarking for Agentic
  Systems*.** Argues runtime log-based flow discovery from traces, then
  alerts when actual flows diverge from expected. Each agent emits
  `flow_checkpoint(...)` so a downstream analyzer can reconstruct the graph.

- **[arXiv 2503.16416][p3] — *Survey on Evaluation of LLM-based Agents*.**
  Five-perspective taxonomy: planning quality, tool-use success,
  generalization, robustness, cost-efficiency. Each gets its own dashboard
  panel in R13.2.

[p1]: https://arxiv.org/abs/2411.05285
[p2]: https://arxiv.org/abs/2503.06745
[p3]: https://arxiv.org/abs/2503.16416

## Security note (for the demo)

`admin/admin` is the Grafana login, set via `GF_SECURITY_ADMIN_PASSWORD` in
`docker-compose.yml`. **That is fine for a lunch-and-learn on a laptop and
unacceptable for anything reachable from a network you don't control.** For
production: change `GF_SECURITY_ADMIN_PASSWORD` to a secret, put Grafana
behind an SSO proxy, and lock the OTLP receiver to mTLS. The collector
config and the docker-compose file are the only two surfaces you need to
edit.

## Resource footprint

Measured on a 16 GB MacBook (Docker Desktop, 8 GB allocated) under live
workload (one query every ~3-5 s):

| Container          | CPU    | RSS      |
|--------------------|--------|----------|
| otel-collector     | 0.1%   | 53 MiB   |
| jaeger             | 0.05%  | 26 MiB   |
| prometheus         | 0.8%   | 37 MiB   |
| loki               | 0.5%   | 48 MiB   |
| grafana            | 0.3%   | 66 MiB   |
| agent-fleet        | 0.4%   | 50 MiB   |
| **Total**          | **~2%**| **~280 MiB** |

Comfortable to leave running all day on a 16 GB machine while you work on
other things.

## Operator runbook

The `runbook/` directory is the engineering-team operator manual a senior SRE
would hand a new on-call. Seven pages, 5,806 words:

1. **[00-readme.md](runbook/00-readme.md)** — index + paper attribution
2. **[01-architecture-overview.md](runbook/01-architecture-overview.md)** —
   why this stack, why this shape, why each component is decoupled
3. **[02-the-agentops-taxonomy.md](runbook/02-the-agentops-taxonomy.md)** —
   the five span types (paper 1) and what each captures
4. **[03-flow-discovery-analytics.md](runbook/03-flow-discovery-analytics.md)**
   — runtime log-based flow derivation (paper 2's 79% finding)
5. **[04-evaluation-harness.md](runbook/04-evaluation-harness.md)** —
   the five evaluation perspectives (paper 3) mapped to dashboard panels
6. **[05-instrumenting-your-agents.md](runbook/05-instrumenting-your-agents.md)**
   — how to add AgentOps spans to a new agent (the canonical attribute
   convention is locked here)
7. **[06-reading-the-dashboards.md](runbook/06-reading-the-dashboards.md)** —
   panel-by-panel tour of all four Grafana dashboards with red-flag thresholds
8. **[07-incident-response.md](runbook/07-incident-response.md)** —
   the runbook the on-call follows: latency spike, hallucination spike,
   cost burn-through. Each scenario has a triage tree and a concrete close.

## Architecture diagrams

High-resolution (≥1920×1080) PNG renders + Mermaid sources, suitable for
slide decks, the runbook, or docent's `figure` scene annotation:

- **[01-stack-architecture.png](diagrams/01-stack-architecture.png)** —
  the docker-compose stack
- **[02-span-taxonomy.png](diagrams/02-span-taxonomy.png)** — the five
  AgentOps span types and how they nest
- **[03-flow-discovery.png](diagrams/03-flow-discovery.png)** — the
  four-stage flow-discovery pipeline (paper 2)
- **[04-evaluation-pentagon.png](diagrams/04-evaluation-pentagon.png)** —
  the five evaluation perspectives mapped to dashboards (paper 3)

## State of the art

What makes this stack worth standing up over a generic OTel demo:

- **AgentOps span taxonomy.** Most OTel deployments capture HTTP request
  spans. This stack captures the *agent-shaped* spans the papers say a
  multi-agent system needs: `plan_step` as the parent intent, `llm_call`
  with prompt/completion token counts and finish reason, `tool_call` with
  args/response/success/error_kind, plus first-class hallucination flags as
  span events. The instrumentation lives in
  [`agents/instrumentation.py`](agents/instrumentation.py) and the locked
  attribute convention is in
  [`runbook/05-instrumenting-your-agents.md`](runbook/05-instrumenting-your-agents.md).

- **Flow discovery, not just trace search.** Per paper 2, every agent step
  emits a `flow_checkpoint(name)` span event. A Prometheus recording rule
  computes `agentops:flow_stability_ratio` (the fraction of traces matching
  the top-1 happy path), surfaced in dashboard 4 alongside the Jaeger
  node-graph view of the orchestrator → researcher / synthesizer call
  graph. When agent behavior drifts (a model update, a prompt regression,
  a tool change), the stability score drops *before* user-visible failures.

- **Cost-efficiency dashboard, not just throughput.** Per paper 3's
  cost-efficiency dimension, dashboard 2 (`llm-cost-budget`) computes
  token spend per trace, attributes it per agent, projects burn-down
  against a configurable daily budget Grafana variable, and turns red
  when projected spend exceeds budget. Bring-your-own per-model rate.

- **Dashboards as code.** All four dashboards live as JSON under
  `grafana/provisioning/dashboards/`, auto-loaded on container start. No
  hand-clicking in the UI, no drift between environments, every change
  visible in git diff.

- **Hermetic operation.** The agent fleet uses a deterministic fake LLM
  (`agents/fake_llm.py`) so the stack runs without a single API call. The
  span shape is identical to a real OpenAI/Anthropic-backed fleet —
  swapping in a real provider is a one-line change in `fake_llm.py`.

- **Recording rules + LogQL queries documented.** The non-trivial
  flow-discovery metric and the cost-attribution queries live in
  [`grafana/recording-rules.md`](grafana/recording-rules.md) with their
  exact PromQL + LogQL bodies, so a copy of this stack into a different
  observability backend (Datadog, Honeycomb, New Relic) has a clear port
  surface.

## How this was built

This repo was authored by [**docent**][docent] — an explanation-film engine
that turns a curated knowledge-base directory into a narrated, animated
lunch-and-learn film. The workflow:

1. **Survey.** docent's explainer mode (`--mode ex`) walks a directory like
   this one. The §10 "FDE / SRE / knowledge-base context" section of
   `survey-explainer.md` enforces the rhetorical bar: identify the
   misconception the film must kill, name the hero diagram, name the hero
   demo, surface the "when this doesn't apply" tension.
2. **Treatment.** docent scaffolds a plain-English treatment markdown the
   author edits — the human-in-the-loop checkpoint.
3. **Spec compile.** `docent treatment <id> --to-spec` reads the treatment's
   asset-binding syntax (`figure: 02-span-taxonomy.png — annotate the
   nested span tree`) and emits a film spec.
4. **Render.** `docent build <id>` produces an MP4 with TTS narration,
   choreographed visualization, scene-aware music ducking, and broadcast-
   compliant LUFS normalization.

The companion lunch-and-learn film for this repo is at
`out/agentops-lunch-and-learn.mp4` in the docent project. It walks an
engineering team through how this stack is engineered, talks through the
Grafana dashboards, and closes with the incident-response decision tree.

[docent]: https://github.com/benelser/archcast

## Layout

```
agentops/
├── README.md
├── docker-compose.yml
├── otel-collector-config.yaml
├── prometheus.yml
├── grafana/
│   ├── recording-rules.md                  # the non-trivial PromQL + LogQL
│   └── provisioning/
│       ├── datasources/datasources.yaml
│       └── dashboards/
│           ├── dashboards.yaml             # provider
│           ├── 01-agent-overview.json
│           ├── 02-llm-cost-budget.json
│           ├── 03-tool-call-success.json
│           └── 04-flow-discovery.json
├── agents/
│   ├── pyproject.toml
│   ├── Dockerfile
│   ├── instrumentation.py                  # AgentOps taxonomy span helpers
│   ├── fake_llm.py                         # deterministic LLM + tools
│   ├── orchestrator.py
│   ├── researcher.py
│   ├── synthesizer.py
│   └── workload.py                         # the continuous driver
├── runbook/                                # 7 pages, 5,806 words
│   ├── 00-readme.md
│   ├── 01-architecture-overview.md
│   ├── 02-the-agentops-taxonomy.md
│   ├── 03-flow-discovery-analytics.md
│   ├── 04-evaluation-harness.md
│   ├── 05-instrumenting-your-agents.md
│   ├── 06-reading-the-dashboards.md
│   └── 07-incident-response.md
└── diagrams/                               # 4 PNG (≥1920×1080) + .mmd sources
    ├── 01-stack-architecture.{mmd,png}
    ├── 02-span-taxonomy.{mmd,png}
    ├── 03-flow-discovery.{mmd,png}
    └── 04-evaluation-pentagon.{mmd,png}
```

## Teardown

```bash
docker compose down -v   # -v wipes the prometheus + grafana volumes
```
