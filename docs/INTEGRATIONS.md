# Native integration runbook

The Flagsmith SDK and REST contracts were checked against official documentation and the public repository's experimentation implementation, but have not been exercised against your account. Experimentation is a beta capability; API/schema/version differences must fail closed. Do not run this against an actual production feature or an environment shared with users.

## 1. Flagsmith setup

Read [official agent setup](https://docs.flagsmith.com/experimentation/set-up-with-a-coding-agent) and [SDK instrumentation](https://docs.flagsmith.com/experimentation/run-an-experiment). Install official CLI v2+ separately, then authenticate locally. Do not paste credentials into chat.

```bash
flagsmith login
flagsmith project list
flagsmith environment list --project <project-id>
flagsmith init --project <project-id> --environment <environment-client-key>
flagsmith feature create firstproof_retry_policy --value control --enabled
flagsmith feature variant add firstproof_retry_policy --value context_aware --key context_aware --weight 0
flagsmith feature get firstproof_retry_policy --json
```

Create the feature once per project, not once per environment. Ensure `control` is the default value and `context_aware` is a **keyed** alternative. Keep candidate weight at zero outside the experiment. Capture numeric project, feature and multivariate option IDs from the official output. Use a dedicated pilot environment with no external traffic and no overrides. Connect a supported warehouse via the native Flagsmith UI: [warehouse docs](https://docs.flagsmith.com/experimentation/connect-a-warehouse). Ensure the warehouse is actually connected before proceeding.

Configure these settings (see `.env.example`):

- `FLAGSMITH_ENVIRONMENT_KEY`: client/server evaluation environment key; for management URL use the client-side environment key as required by native endpoints.
- `FLAGSMITH_ADMIN_KEY`: authorized management API key with the narrowest necessary environment permissions; not the SDK key.
- `FLAGSMITH_PROJECT_ID`, `FLAGSMITH_FEATURE_ID`, `FLAGSMITH_CANDIDATE_ID` from the actual project.
- `FLAGSMITH_API_URL`: includes trailing `/api/v1/`.
- `FLAGSMITH_EVENTS_URL`: leave empty for Cloud; self-host must set correctly.
- `FLAGSMITH_DASHBOARD_URL`: browser-accessible dashboard origin.

FirstProof uses the management API to create a run-unique occurrence metric `operation_verified_<uuid>` and experiment, start it, inspect the stored rollout, refresh and read results/exposures, and complete the experiment with a native rollout decision. Its target binding hash prevents an old approval from being applied to a differently configured environment. Creation of the reusable feature and its keyed alternative is an explicit one-time operator prerequisite, not an agent guess.

### Native API endpoints used

All paths are relative to `/api/v1/environments/<environment-key>/`:

| Operation | Endpoint |
|---|---|
| Warehouse check | GET `warehouse-connections/` |
| Metric | POST `experiment-metrics/` |
| Native experiment | POST `experiments/` |
| Inspect | GET `experiments/<id>/` |
| Activate | POST `experiments/<id>/start/` |
| Refresh | POST `experiments/<id>/results/refresh/` and `exposures/refresh/` |
| Read | GET `experiments/<id>/results/` and `exposures/` |
| Complete | POST `experiments/<id>/complete/` with `rollout_decision=control` or `variant` and `rollout_variant=<id>` |

SDK reads an identified flag, checks enabled/enrolled status, native experiment ID, actual value and variant key, then records the explicit exposure. Conversion is emitted only after the separate verifier succeeds. All logical conversions have a durable outbox row. The SDK's flush is **not** proof of delivery, and the local state `sdk_submitted` does not claim delivery. Native result/exposure counts and quarantined identities are checked against local observations. Unknown or mismatching schema prevents promotion.

Reference implementation: [Flagsmith experiment views](https://github.com/Flagsmith/flagsmith/blob/main/api/experimentation/views.py), [serializers](https://github.com/Flagsmith/flagsmith/blob/main/api/experimentation/serializers.py), [result data classes](https://github.com/Flagsmith/flagsmith/blob/main/api/experimentation/dataclasses.py), [statistics](https://github.com/Flagsmith/flagsmith/blob/main/api/experimentation/stats.py), [Python SDK models](https://github.com/Flagsmith/flagsmith-python-client/blob/main/flagsmith/models.py). Consult your installed version if the beta response shape differs. Do not loosen the result gate just to make a demo look successful.

## 2. Pilot and main run

Pilot: select synthetic calibration, 120–200 identities (reporting minimum is 50 per arm and at least 5 conversions per arm; it is not a power guarantee). If counts do not reconcile, investigate the warehouse, keyed variations and event ingestion. A small pilot may remain inconclusive. Complete with control retained after reconciliation. If the native experiment was interrupted, inspect its journal and native console; do not rerun blindly. See the recovery section in deployment docs.

Main: configure a distinct native environment. Use SerpAPI/OpenRouter evidence, examine the proposed scenario mapping and select a sample count justified by pilot variance, expected effect and the practical threshold. `scripts/sample_size.py` supplies only a rough nonzero-effect approximation. Run main once with a fixed seed and immutable approved protocol. Do not repeatedly choose seeds or weights until the candidate wins. The separate reserved validation uses a predeclared deterministic holdout seed and both policies on paired profiles; it is a limited regression check, not a statistically independent production sample or truly hidden holdout.

Results refresh is asynchronous. Wait for ingestion, click `Request native refresh`, then `Read native results` after computation. Use the native console to confirm. Respect the provider refresh interval (documentation describes a five-minute results cooldown). A valid negative or inconclusive result is a valid experiment; do not manipulate it.

Completion promotes or retains the selected behavior in the **experiment sandbox only**; readback and a controlled smoke check verify it without redeployment. A separate configured release environment may drive live research requests, but this build does not automatically mutate it. Use native approval workflows for any later production migration. No automatic live rollout.

## 3. SerpAPI and OpenRouter

Set `SERPAPI_KEY` and `OPENROUTER_API_KEY`; choose an available `OPENROUTER_MODEL` supporting JSON schema output. The default model slug is a candidate, not a verified availability guarantee. The adapter requests strict schema output and rejects bad output. Set appropriate account spending limits. Search calls use `https://serpapi.com/search.json` and a read-only client; the exact same policy functions run against local FaultLab. Normal search uses a longer timeout than the controlled benchmark; performance cannot be generalized across them.

FirstProof searches twice (support and counterexample intent), reserves four possible provider attempts per query from a global conservative database allowance, then retrieves only exact public GitHub issue URLs through the fixed GitHub API origin. A search's label describes retrieval intent, not verified agreement with a hypothesis. The model proposes hypotheses, scenario weights and source hashes; the operator must inspect their substantive relevance. Raw remote pages cannot invoke tools or select URLs. API errors are reported, not replaced with invented evidence. Live search may consume credits even when requests are unsuccessful; the counter is a conservative reserved-attempt budget rather than actual billing.

## 4. Self-hosted Flagsmith (Track 03 supporting capability)

Follow the official [self-hosting experimentation guide](https://docs.flagsmith.com/experimentation/self-hosting) and [local installation guide](https://docs.flagsmith.com/getting-started/run-locally-with-a-coding-agent). Select a verified release that supports Experimentation; enable `EXPERIMENTATION_ENABLED=true` on its API as documented and configure its Flagsmith database evaluation-oriented warehouse option (or a supported dedicated warehouse). Do not expose its administrative API publicly. Make its browser/API port distinct from FirstProof (e.g. host 8080). Set:

```dotenv
FLAGSMITH_API_URL=http://host.docker.internal:8080/api/v1/
FLAGSMITH_EVENTS_URL=http://host.docker.internal:8080/api/
FLAGSMITH_DASHBOARD_URL=http://localhost:8080
```

```bash
docker compose -f compose.yaml -f compose.selfhosted.yaml up --build
```

This overlay connects FirstProof to an **already installed** Flagsmith server. It does not install or pin Flagsmith itself; its release image and configuration must be verified before submission. On a shared private Docker network, prefer the service name instead of host gateway. Never leave the self-hosted events URL at its cloud default. Validate actual warehouse records and native results before describing self-hosting as tested.
