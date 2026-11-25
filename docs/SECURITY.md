# Security threat model

Aligned in design with OWASP ASVS and OWASP API/LLM Top 10, not a certification. The intended deployment is a single-operator experiment sandbox. Never use actual production credentials or expose native administration to the public. No promise of zero vulnerabilities.

| Risk | Implemented control | Required deployment/test work |
|---|---|---|
| Unauthorized mutations | Random operator token, signed expiring session, CSRF token, strict Origin, state checks, hash binding | TLS, rate-limit login, rotate/revoke token if leaked; shared single operator not full RBAC |
| SSRF | Exact GitHub issue pattern translated to fixed API host; fixed provider hosts; redirects off; trusted operator-only configured URLs | Restrict network egress at infrastructure layer |
| Prompt injection | Model output is restricted JSON, local typed validation, known source hashes, human approval; no model tool privileges | Adversarial live model test and human source review |
| XSS | Jinja autoescape, no raw HTML rendering, CSP and static local assets | Browser and ZAP test; validate configured links |
| CSRF | Same-origin POST and session-bound token; no GET mutation | Verify proxy forwards expected origin |
| Secret leakage | Server env, no keys in templates or exports, provider errors sanitized, access logs disabled in service commands | Secret scan; verify deployment logging configuration |
| Unbounded resource use | Queue cap, concurrency limits, source response cap, conservative global search budget, request body cap | Proxy limits and multi-tenant quotas; provider spend limits |
| Experiment manipulation | Immutable canonical protocol hash, native enrollment/rollout readback, local trace verifier, result hash approval | Native identity-level audit if required; protect DB and operator credentials |
| Replay / stale approvals | State transition lock, expiring cookie, result/protocol hash, target fingerprint | Additional server-side session revocation for broader deployment |
| Dependency compromise | Version ranges, CI dependency scan | Resolve reproducible lockfile and review exact dependency versions before production |

Run `bandit -r firstproof -ll`, `pip-audit`, a secret scanner (Gitleaks), container scanner (Trivy), and ZAP against your own deployment. No scans were performed here beyond the automated regression tests; network package install was unavailable. Redact logs before sharing. Public publication explicitly exposes the reviewed protocol, evidence excerpts and aggregate measurements; review for sensitive text before publishing. Do not submit credentials or proprietary incident text to search or model providers. Model-provider data handling must be checked against account policies.
