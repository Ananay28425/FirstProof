# FirstProof

**Evidence-driven release verification for Flagsmith.**

**Can one rate limit stall two customers?** FirstProof compares a shared client cooldown with a workspace-and-method cooldown. A controlled upstream rate-limits Workspace A. Flagsmith assigns each two-workspace episode to one policy, and FirstProof measures whether the healthy workspace finishes within 500 ms while the throttled workspace still recovers. Inspect the real HTTP traces and use native results with an independently checked release gate to decide whether to promote.

Local replays use the same HTTP runner and a labeled local allocator. The nominal workload mix describes a controlled study of the modeled shared-client behavior. It does not estimate the prevalence of affected Slack integrations.

## Quick start (Windows / macOS / Linux)

Requires Python 3.12+ (3.12 recommended). Open a terminal in this directory. The local script generates an operator token and uses loopback-only ports 8000/8001 and SQLite. No API keys are required for the synthetic replay.

```powershell
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[dev]"
python scripts/local.py
```

Open **http://127.0.0.1:8000** and sign in with the token printed in your terminal. Select **Workspace cooldown · pinned Slack sources**, leave 30 workloads and approve the protocol. Watch real measured outcomes. Export raw JSON or publish the read-only report. Ctrl+C stops all three processes. The local database `local-replay.db` persists; delete it only if you intend to discard previous replays.

The local profile uses a clearly labeled test double for allocation; it cannot authorize a native promotion. If the SDK is not available on your current package index, the local-only install is:

```bash
python -m pip install fastapi uvicorn pydantic pydantic-settings sqlalchemy httpx jinja2 python-multipart alembic pytest pytest-asyncio
python scripts/local.py
```

This is an offline-development fallback (packages still need to be available locally or via a package index). The live SDK must not be omitted from a native deployment.

## Real Flagsmith experiment (required for Track 01)

Follow [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md) in order. You need a Flagsmith environment with Experimentation enabled and a connected warehouse, a keyed multivariate feature, appropriate management credentials, and a dedicated PostgreSQL database. The workspace study uses pinned sources and requires no Slack, SerpAPI, or OpenRouter credentials. The repository never contains real keys.

1. Run a disposable native pilot in a dedicated Flagsmith environment; complete it with control retained.
2. Set up a **different** native environment for the frozen main study. Inspect the credible baseline, pilot metrics, sample-size assumption, scenario weights and guardrails. Do not tune on the main or reserved validation results.
3. Set `.env` from `.env.example`, fill in the required IDs/keys and `MODE=live`, then run the database migration and web/worker/FaultLab services.
4. Select the pinned workspace protocol and prepare a main draft, inspect the evidence and approve it. FirstProof provisions the native occurrence and latency metrics and experiment, activates it, runs the episodes, records SDK exposures, verified conversions and B’s elapsed time, and stores raw observations.
5. Request native result and exposure refresh after events arrive; respect the server's refresh cooldown. Read results again once available. FirstProof compares aggregate native counts with actual local observations; it does not claim per-identity warehouse verification.
6. Approve native completion (retain control or, only when all gates pass, promote candidate). FirstProof reads back Flagsmith state and replays a fresh overlapping episode without deploying new code. The release is confined to the dedicated sandbox.

## PostgreSQL / Docker Compose

Create `.env` from the example. Generate random URL-safe values for `POSTGRES_PASSWORD`, `OPERATOR_TOKEN`, and `FAULTLAB_TOKEN`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Use a new generated token for each. Do not put them in the repository, logs or demo video.

```bash
docker compose up --build
# UI: http://127.0.0.1:8000
```

The migration service seeds schema and admission counters before web/worker start. PostgreSQL and FaultLab are not publicly bound. For live remote deployment configure HTTPS, `PUBLIC_ORIGIN`, `SECURE_COOKIES=true`, an edge proxy with request size and rate limits, and persistent managed PostgreSQL. Scale web and worker processes separately after actual load tests; FaultLab is single-process and must not be replicated without run affinity or external state. See [DEPLOYMENT.md](docs/DEPLOYMENT.md).

## What is implemented

| Module | Responsibility |
|---|---|
| `config.py` | Explicit safe settings and fail-closed live requirements |
| `contracts.py` | Typed immutable protocol, evidence and schema constraints |
| `db.py` | Relational records, transactions, leased queue and budget |
| `faultlab.py` | Private time-based controlled upstream and independent server trace |
| `cooldown.py` | Two-workspace episode execution, policy-scoped state and trace comparison |
| `policies.py` | Two actual read-only HTTP policies sharing correct retry semantics |
| `verification.py` | Deterministic contract checks, raw metrics and conservative release policy |
| `integrations.py` | Native Flagsmith SDK/admin API, real SerpAPI and OpenRouter |
| `worker.py` | Durable research/execution/reconciliation/completion workflow |
| `web.py` | Same-origin operator UI, approvals, read-only publication and export |
| `migrations/` | Alembic schema and counters |
| `tests/` | Local safety tests, schema fixtures, true loopback HTTP replay |

## Why Flagsmith is the lead technology

Removing Flagsmith in live mode disables enrollment, verified native conversions, native statistics and native completion. The runner refuses to fall back to local assignment in live mode. SerpAPI and OpenRouter only propose evidence-backed scenarios; neither determines success nor authorizes mutation. PostgreSQL preserves independently measured outcomes. See [ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Test and reproduce

```bash
python -m pytest -q
python -m compileall -q firstproof
# Use pilot estimates and a predeclared practical effect for sample sizing.
python scripts/sample_size.py --baseline 0.55 --delta 0.10
# After starting the owned service; k6 installed separately:
k6 run benchmarks/read-spike.js
```

Use `TEST_POSTGRES_URL` with a **disposable test database** to exercise queue tests on PostgreSQL; the suite drops its test tables. Never point it at data you want to retain. The checked-in k6 workload targets read endpoints only, not external providers. The code does not claim measured load capacity or security certification. Read [VALIDATION.md](docs/VALIDATION.md) for evidence and blockers.

## Hackathon scope

Official target: Track 01. Track 02 functionality supports it through evidence research, schema-bound planning, native API operations and human approval. Track 03 compatibility is through the native self-hosted API/events endpoints and documented upstream installation; it is not a separately submitted track. The final demo must show **actual** Flagsmith results, not replay charts or test fixtures.

## License and methodology

MIT license. Weaponized Curiosity informs the research and falsification methodology, not an unbounded runtime agent. See [AGENTS.md](AGENTS.md) and the upstream [skill](https://github.com/ayushrajsinghparihar/weaponized-curiosity).
