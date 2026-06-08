# AgentOps

Production-ready observability for LLM agent systems, distilled from three
arXiv papers ([2411.05285][p1], [2503.06745][p2], [2503.16416][p3]).

A complete OpenTelemetry stack — collector, Jaeger, Prometheus, Loki, Grafana
— plus a three-agent Python fleet that emits the *exact* taxonomy of telemetry
the papers argue an agent system needs to be safe in production. Clone it,
`docker compose up`, open Grafana, watch agents work in real time.

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

On a 16 GB MacBook the whole stack at idle uses **~1.3 GB RAM** and **<5%
CPU** averaged across cores. Under the demo workload (one query every
~3-5 s) it climbs to **~1.8 GB RAM / ~12% CPU**. Comfortable to leave running
all day while you work on other things.

## Layout

```
agentops/
├── README.md
├── docker-compose.yml
├── otel-collector-config.yaml
├── prometheus.yml
├── grafana/
│   └── provisioning/
│       ├── datasources/datasources.yaml
│       └── dashboards/dashboards.yaml      # provider; JSONs land here in R13.2
└── agents/
    ├── pyproject.toml
    ├── Dockerfile
    ├── instrumentation.py                  # AgentOps taxonomy span helpers
    ├── fake_llm.py                         # deterministic LLM + tools
    ├── orchestrator.py
    ├── researcher.py
    ├── synthesizer.py
    └── workload.py                         # the continuous driver
```

## Teardown

```bash
docker compose down -v   # -v wipes the prometheus + grafana volumes
```
