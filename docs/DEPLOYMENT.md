# Deployment and failure recovery

## Single-host production-oriented demo

Use the Compose stack with a managed TLS reverse proxy or host service. Restrict operator access using the proxy, enforce request-size and request-rate limits, set a randomly generated operator token and secure cookies, and do not publish the database or FaultLab. The included Compose stack deliberately binds the web app to loopback only. Run the migration before web and worker. Avoid sleeping instances during judging.

For a hosted platform (e.g. Render) use a web service with the Docker image, a separate background worker command `python -m firstproof.worker`, managed PostgreSQL, and a private service `uvicorn firstproof.faultlab:app --host 0.0.0.0 --port 8001 --no-access-log`. Use the internal FaultLab URL, not its public address. Run `python -m alembic upgrade head` once as the pre-deploy migration step. All services need server-only settings; use environment secret management. This layout is documented, not deployed here. Render plan capabilities and costs must be checked before purchase.

## Recovery

If the worker stops before claiming a job, its accepted queued job persists. A lost lease marks a running job `failed` and the associated experiment `interrupted`; it is never automatically rerun with duplicate exposure. Inspect the audit, operator raw export, pending remote action and native console. The current build intentionally blocks new native runs in that application database while an interrupted native experiment is unresolved. Resolve the native state manually (retain incumbent if in doubt) and use a fresh isolated application database/environment for the next experiment. Do not change or delete original observations. A future recovery UI should reconcile exact native resources and record a reviewed closure in the existing database; it is not implemented here.

If the native API is unavailable, stop assignments; existing measured local rows and outbox state remain. The remote journal may show an unknown outcome if a request failed after dispatch. If native counts do not match, block candidate promotion. Never fabricate missing conversions. Results refresh errors can be retried after the native cooldown. If a completion call succeeds but readback/smoke fails, the run is interrupted: inspect native state; do not retry a mutation blindly.

SerpAPI or OpenRouter failure leaves the draft with an explicit error; synthetic replay is not a substitute for main live evidence. A user can start a new draft. No model calls are needed for already approved execution. PostgreSQL downtime should cause a ready-check failure and the host should stop admitting requests. The web process does not return 202 before its job transaction commits; the HTML form redirects after the durable state change.

## Load validation

Run the included k6 read spike on an owned test deployment. Record request rate, hardware, memory, database connections, p95, error rate, intentional 429/503 and recovery. Benchmark expensive jobs using test doubles for external APIs, not live providers. The sample script only covers ordinary read endpoints; add mixed job and recovery load tests before claiming capacity. Targets are p95 <200ms for read routes, <300ms for queue admission, <1% unexpected errors under a defined normal workload, and no lost accepted records during worker disruption. These are targets, not achieved claims. Set a public rate limit and resource limits before any open deployment.
