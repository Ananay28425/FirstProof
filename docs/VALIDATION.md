# FirstProof validation

## Workspace cooldown study

- **31 tests passed** on Python 3.12.3. The suite covers real loopback replay of both studies, overlap and non-overlap behavior, native event contracts, missing or invalid sample ratio diagnostics, and the Workspace A recovery guard. Uvicorn's WebSocket dependency emitted two deprecation warnings.
- **Chrome smoke passed** for operator sign-in, approval, 30 two-workspace HTTP episodes, trace display, raw export, read-only publication and mobile rendering. It used local assignment.
- **PostgreSQL 17 passed** Alembic upgrade and schema check, plus the lease-recovery and duplicate-claim test against a disposable database.
- **Native Flagsmith passed end to end** in a disposable self-hosted sandbox on November 27, 2026. Python SDK 6.7.0 assigned 160 HTTP episodes. The exposure and occurrence counts reconciled, and Flagsmith returned the numeric latency metric. Workspace B finished within 500 ms in 46 of 87 shared episodes and 73 of 73 scoped episodes, a gain of 47.1 percentage points. Workspace A completed every episode. The candidate's average upstream attempts increased by 4.0%, below the 10% guardrail.
- **The release path passed.** Flagsmith reported a 99.9998% chance to beat control. The independent exposure ratio check returned p = 0.2684 because the native diagnostic was unavailable. The reserved validation passed across 20 matched pairs. A test approval through the operator workflow completed the candidate rollout. FirstProof read back the applied flag and verified B's fresh read in 33.9 ms. Workspace A also succeeded in that replay.

The native sandbox used Flagsmith image digest `sha256:48905c664152d8ae2c843ec9cee72ff197e7caff6e91b959bb851a235915ea35`. The run verifies the integration and release flow on controlled workloads. The official study and submission are pending.

## Original search-client study — development build 0.1.0

This report distinguishes actual measurements from unexecuted checks. No credentials or native experiment observations are included.

## Executed checks

- `python -m pytest -q`: **27 passed** (Python 3.13.5, 5.67 seconds; two dependency deprecation warnings from Uvicorn/websockets). Includes actual local HTTP end-to-end replay with 14 workloads; session and CSRF protection, private report access, approval hash enforcement, duplicate run prevention, raw export, read-only publication, fault traces, malformed response rejection, excessive Retry-After, queue expiry and native API/SDK contract test doubles.
- `python -m compileall -q firstproof scripts migrations`: passed.
- Alembic upgrade, `alembic check` (no new operations), and downgrade against a fresh disposable SQLite database: passed.
- YAML parsing of Docker Compose files and CI workflow: passed (syntax only).
- Official Flagsmith docs and implementation examined for the native SDK and API wire contract. Test doubles check the requested paths/payloads, not provider execution.

## Not completed / blockers

- Live Flagsmith event ingestion, native results and actual completion: not run; credentials and Experimentation access absent. The API is beta; validate against your actual release.
- Live SerpAPI and OpenRouter: not run; credentials absent. The default model slug and exact supported schema must be tested.
- PostgreSQL concurrency, Docker build/Compose launch and self-hosted Flagsmith: not run; local environment has no Docker or PostgreSQL runtime/driver. CI includes a disposable PostgreSQL service for queue tests.
- Dependency locking: attempted package resolution failed because PyPI DNS/network access was unavailable. `pyproject.toml` has bounded ranges, not a verified `uv.lock`. Resolve and review an exact lockfile before a production release.
- Chromium smoke script: attempted; the supplied browser blocked loopback navigation with `ERR_BLOCKED_BY_ADMINISTRATOR`. Browser journey and visual rendering are unverified. HTTP template rendering was tested. Run `python scripts/browser_smoke.py` on your own machine after installing Playwright/Chromium.
- k6, Bandit, pip-audit, Gitleaks, Trivy and ZAP: not executed. No measured high-load capacity, penetration test or security certification.
- No GitHub push or deployment was performed. No official hackathon submission was made.

## Release gate

Do not present the code as an official experiment submission until the live pilot, main native experiment, event reconciliation, source-backed protocol, native completion, and actual demo have passed. A reproducible negative/inconclusive result is acceptable. An unsupported or fabricated result is not.
