# FirstProof 0.1 — functional requirements

| ID | Requirement | Implementation status |
|---|---|---|
| FP01 | SerpAPI evidence discovery | Adapter implemented; live untested |
| FP02 | Source verification and ledger | Fixed GitHub issue adapter; live untested |
| FP03 | Evidence-to-scenario compilation | Typed proposal, known hashes and allowlisted scenario families; provider untested |
| FP04 | Frozen protocol, human approval | Implemented; local tests pass |
| FP05 | Native Flagsmith provisioning | Native metric/experiment/start implemented; one-time keyed flag setup required; live untested |
| FP06 | Actual alternative retry implementations | Implemented; local tests pass |
| FP07 | Controlled execution and independent verifier | Implemented; real loopback test passes |
| FP08 | Native results + count reconciliation | Implemented; contract test doubles pass; native untested |
| FP09 | Release guard and human approval | Implemented for sandbox; native completion untested |
| FP10 | Reproducible deployment and documentation | Local run script tested by HTTP workflow, migrations checked; Docker/self-host not verified |

Nonfunctional goals: bounded concurrency and queue admission, persistent jobs, low read latency, secure-by-default authorization, no arbitrary agent code, reproducibility. Scale and latency targets are unmeasured; multi-tenant authorization, external high availability and infrastructure security controls are not part of this initial single-operator version. See SECURITY and DEPLOYMENT.
