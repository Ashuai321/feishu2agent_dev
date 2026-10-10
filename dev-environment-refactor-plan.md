# Development Environment and Architecture Refactor Plan

## Document status

This document proposes a reviewable plan only. It does not authorize or perform implementation, Cloudflare changes, secret changes, production deployment, or a pull request. The repository owner should review the scope and unresolved decisions before implementation begins.

## 1. Objective

Refactor the development environment and application structure of `feishu2agent_dev` in line with the applicable CardSpending project, coding, and agent-work standards, while retaining the existing Cloudflare Python Worker, D1, and Queue platform.

The intended result is a reproducible local development setup and an isolated remote development environment, with clearer application layers, deterministic checks, and a documented deployment and review workflow. The production environment, production data, public API contracts, and user-visible behavior are outside this plan's change scope.

## 2. Guideline mapping

CardSpending's FastAPI, PostgreSQL, SQLAlchemy, and GCP deployment choices are specific to that project and should not be copied into this Cloudflare project. The transferable standards are:

- Keep request and event entry points thin. Put business rules in service modules and external-system access behind adapters or repositories.
- Keep environment differences in explicit configuration, and run the same compatible application runtime across local, development, and production environments.
- Use a single approved source for application secrets, apply least privilege, and never put secret values in source code, logs, or documentation.
- Use explicit types at module boundaries, small focused changes, readable English engineering documentation, and deterministic automated checks.
- Validate real external-system state instead of inferring it from resource names. Preserve owner review and approval boundaries; do not open a pull request or merge without the owner's explicit direction.
- Keep changes scoped, review the complete diff, and document operational, migration, retention, and rollback considerations.

## 3. Observed repository baseline

The plan was prepared against branch `shuai_review` at commit `9eba5e681066dc1d861dac66a382f69f02d0289d`. The working tree used to prepare this document was clean. The branch contains a DEV deployment mirror commit above the observed `main` tip; implementation should re-check both branch tips before starting and preserve the review branch's intended base.

The checked-in project uses a Cloudflare Python Worker with D1 and Queue bindings. The root Wrangler configuration currently describes a single Worker configuration rather than a clearly named `dev` environment. The application entry point imports the Worker class from `worker_app.py`. The main implementation files are large: `worker_app.py` (about 6,840 lines), `xiaot_worker.py` (about 2,405 lines), and `agent_config_mcp.py` (about 2,027 lines). There are 14 D1 migration files and existing pytest coverage for Worker routes, webhooks, OAuth, queue handling, MCP, and XiaoT flows. Ruff is configured. The repository declares Python 3.11 compatibility and has a `.python-version` file, but the checked-out branch does not contain a committed `uv.lock` or CI workflow under `.github/workflows`.

Repository documentation describes local `pywrangler dev`, remote deployment, and DEV-specific flows. Names and checked-in configuration alone do not prove which resources are currently active in Cloudflare. Before implementation, verify actual Worker routes, D1 database IDs, Queue bindings, secrets, and deployment state using read-only account inspection.

The value named `LARK_DOCUMENT_WIKI_TOKEN` appears in configuration and code fallback paths. Its classification as a credential or a non-secret identifier is unresolved. Do not copy, print, log, rotate, or alter it until its purpose and approved source have been confirmed. This plan intentionally omits its value.

## 4. Scope and invariants

### Included

- A local development profile using the existing Python Worker toolchain and local/simulated D1 and Queue bindings.
- A remote DEV Worker isolated from production, with dedicated D1, Queue, hostname, application credentials, and test identities.
- Incremental separation of HTTP/queue entry points, business services, platform integrations, persistence, and queue orchestration.
- Configuration validation, types, lint/format/test gates, dependency reproducibility, and English engineering documentation.
- DEV-only deployment and smoke validation after the owner approves implementation and provisions any required resources.

### Excluded

- Replacing Cloudflare with FastAPI, PostgreSQL, SQLAlchemy, GCP, or another runtime/platform.
- Production Worker, production routes, production secrets, production D1 data, and production deployment.
- Changing externally visible routes, webhook payloads, OAuth behavior, queue message contracts, or user-facing behavior as part of the structural refactor.
- Copying production data, OAuth tokens, or real user messages into DEV.
- Enabling R2; retain the current disabled/unbound posture unless a separate approved need and cost review establish otherwise.
- Automatic pull request creation or merging. The owner requests a PR explicitly and performs the merge.

## 5. Proposed implementation sequence

Each phase is a review checkpoint. Keep changes small enough to review and revert independently. Stop and return to the owner if a gate fails or scope must expand.

### Phase 0 — Protect the baseline and verify assumptions

1. Record the target branch, commit, tracked/untracked state, remote branch tips, and current configuration before editing. Do not discard, overwrite, or absorb unrelated work.
2. Read repository instructions and current CardSpending standards again at implementation time; record any conflicts or project-specific exceptions.
3. Inspect Cloudflare state read-only: Worker names/routes, D1 IDs, Queue names and bindings, deployment versions, secret names (never values), and relevant account/environment ownership. Compare identifiers to determine whether current DEV resources are actually isolated.
4. Trace the use and source of `LARK_DOCUMENT_WIKI_TOKEN` without displaying its value. Classify it with the owner. If it is a credential, identify the approved secret source and rotation/consumption model; if it is an identifier, document that classification and any sensitivity controls.
5. Record existing HTTP routes, webhook event shapes, OAuth state lifecycle, queue payloads/retry behavior, platform-specific prefixes, and representative current test outcomes as compatibility baselines.

**Gate:** no resource or secret change begins until production/DEV separation and secret handling are understood. Any ambiguous binding or credential blocks the affected change, not the rest of the read-only inventory.

### Phase 1 — Design isolated local and remote DEV configuration

1. Define environment-specific names and bindings for the DEV Worker, workers.dev/custom test hostname, D1 database, Queue, OAuth callback URLs, and Feishu/Lark/Workspace Agent test applications. Use dedicated synthetic/test identities.
2. Choose a Wrangler named environment if it can express the required Python Worker configuration and binding isolation reliably; otherwise use a separate checked-in DEV configuration. Do not rely on undocumented defaults.
3. Keep local `pywrangler dev` behavior reproducible with local/simulated D1 and Queue bindings. Document any local-only values as non-secret examples and load credentials through an approved developer workflow.
4. Use a compatible Python Worker runtime and application artifact across environments; represent differences through bindings and configuration rather than divergent application code.
5. Provision remote DEV resources only after the owner approves the inventory and cost/ownership implications. Never point DEV at production D1, Queue, routes, or credentials.

**Gate:** a configuration review and automated isolation check must demonstrate that DEV references only DEV resources before a remote deployment is attempted.

### Phase 2 — Establish configuration and secret safeguards

1. Add centralized configuration parsing and validation for required bindings and environment-specific settings. Fail safely when a required value is absent or inconsistent.
2. Add checks that compare DEV and production resource identifiers and reject accidental production bindings in DEV configuration. Keep expected identifiers in non-secret configuration where practical.
3. Validate whether Cloudflare Workers can securely obtain the approved secret source using a narrowly scoped, short-lived/bootstrap identity. Prefer federation or short-lived authorization where technically supported.
4. Do not place application credentials in source defaults, checked-in files, logs, or a second persistent copy in Cloudflare. If the approved secret integration cannot be demonstrated, pause remote secret migration and ask the owner to select an approved exception; do not silently fall back to hard-coded values.
5. Document secret names, ownership, least-privilege access, local developer access, rotation, and incident handling without documenting secret values.

**Gate:** secret source, identity scope, runtime retrieval, and failure behavior are proven before changing any existing credential path.

### Phase 3 — Refactor modules incrementally, preserving contracts

Refactor one behavior area at a time and preserve existing imports temporarily where needed to limit risk.

1. Keep `entry.py` and the HTTP/Queue Worker handlers focused on protocol parsing, authentication/validation, dispatch, and response construction.
2. Extract application services for the existing behavior groups: Feishu/Lark webhook processing, OAuth flows, Agent configuration/MCP requests, Bitable operations, XiaoT workflows, and asynchronous job orchestration.
3. Put Feishu, Lark, Workspace Agent, and other external API clients behind explicit integration adapters. Keep D1 reads/writes behind repository or storage modules and Queue publishing/consumption behind queue adapters.
4. Define typed request, event, job, and result structures at module boundaries using dataclasses or Pydantic only where it fits current dependencies and runtime constraints. Add annotations to public functions without introducing a broad new abstraction layer.
5. Preserve route names and aliases, webhook verification/challenge handling, OAuth callback and one-time state semantics, queue payload compatibility, idempotency, platform separation, same-sender confirmation rules, and existing user-visible responses.
6. Replace broad fallback behavior and unsafe logs only when behavior is covered by tests. Logs must omit tokens, authorization headers, message bodies, and other sensitive values while retaining useful correlation identifiers.
7. Avoid unrelated cleanup, speculative generic frameworks, and large one-shot file moves. Each extraction should have a clear boundary and a small diff.

**Gate:** targeted tests for the extracted behavior pass before the next behavior group is moved. No test or refactor should change a contract without a separately reviewed proposal.

### Phase 4 — D1, Queue, and dependency reliability

1. Inventory and document all 14 current D1 migrations, their ordering, and any migration assumptions. Apply schema changes additively and keep old and new Worker versions compatible during rollout.
2. Exercise a fresh isolated DEV database and an upgrade from the current DEV schema. Do not use production data as a fixture.
3. Verify queue at-least-once delivery, retries, duplicate delivery, idempotency, poison-message handling, and observability. Ensure a retry cannot repeat an externally visible action unexpectedly.
4. Define D1 data retention and deletion rules for OAuth state, tokens, job records, and test data. Avoid logging or exporting personal data in test artifacts.
5. Align `.python-version`, `requires-python`, Wrangler/Python Worker support, and dependency tooling. Select one authoritative Python lockfile and update it through the declared package manager; do not commit multiple competing lockfiles.
6. Review dependency changes for runtime compatibility and cost/size impact. Keep R2 disabled.

**Gate:** migration and queue behavior are validated in isolated DEV, and rollback compatibility is documented before deployment.

### Phase 5 — Deterministic quality gates and CI

1. Establish a repeatable local command set for Ruff lint, Ruff format check, and pytest, with test credentials/services replaced by fixtures or fakes.
2. Add CI checks for lint, format, targeted pytest, dependency/lock consistency, and DEV configuration isolation. Add a packaging or Worker build check only if supported by the repository's actual Python Worker toolchain.
3. Keep tests deterministic and independent of live Feishu/Lark/Cloudflare services by default. Remote smoke tests are a separate, explicit DEV-only job using dedicated test identities.
4. Run the smallest relevant test selection after each change. Run the full agreed suite only at the owner-approved integration checkpoint; document results and failures without hiding skipped checks.

**Gate:** required local and CI checks pass on the proposed DEV branch before remote DEV deployment.

### Phase 6 — Documentation, DEV deployment, and handoff

1. Update or add English engineering documentation for local setup, environment variables, DEV resources, secret access, migration procedure, queue behavior, data retention, cost/ownership, and incident/rollback steps.
2. Deploy manually to the isolated DEV Worker from the agreed branch only after configuration, secret, tests, and migration gates pass. Record the deployed commit and resource identifiers (not secret values).
3. Run DEV smoke checks for health, representative Feishu/Lark webhooks, OAuth state/callback, queue retry/idempotency, MCP dispatch, and XiaoT confirmation using synthetic data and test accounts.
4. Confirm that no production Worker, route, database, Queue, credential, or data was changed. If any isolation check fails, stop deployment and restore the prior DEV configuration.
5. Present the final diff, check results, deployed DEV commit, unresolved items, and rollback steps to the owner for review. Create a PR only if explicitly requested. The owner reviews and merges.

**Gate:** DEV verification is evidence for a later production proposal only. Production changes require a separate explicit scope and approval.

## 6. Risk register and mitigation

| Risk | Impact | Mitigation and response |
|---|---|---|
| DEV accidentally binds production Worker routes, D1, Queue, or credentials | Production data exposure, duplicate actions, or outage | Verify actual Cloudflare state read-only first; use dedicated resource IDs and test identities; add automated config assertions; block deployment on any collision; never copy production data. |
| Secret source cannot be reached securely from the Worker runtime | Credential exposure or service outage | Prove the approved short-lived/federated access path before migration; enforce least privilege and sanitized errors; if unsupported, pause and request an owner-approved exception. Do not hard-code or persist a second app-secret copy. |
| `LARK_DOCUMENT_WIKI_TOKEN` is misclassified | Secret leakage or broken API calls | Trace references without printing the value; have the owner confirm whether it is a credential or identifier; do not change it before classification. |
| Large module extraction changes behavior | Webhook, OAuth, MCP, or user workflow regressions | Extract by behavior group; retain compatibility wrappers while needed; codify current routes and payloads; run targeted deterministic tests at each checkpoint. |
| D1 migration is incompatible with deployed Worker versions | Data loss or failed rollback | Use additive, forward-compatible migrations; test fresh and upgrade paths against isolated D1; document backup/export and stop conditions; keep the previous Worker version compatible. |
| Queue retries repeat side effects | Duplicate messages or external actions | Test at-least-once and duplicate delivery; preserve idempotency keys and retry semantics; define poison-message handling and safe observability. |
| Python Worker/Wrangler or dependency versions differ across environments | Local/remote behavior divergence or deployment failure | Declare supported Python/tool versions, use one authoritative lockfile, validate packaging in CI, and promote the same reviewed artifact/configuration approach across environments. |
| Remote DEV resources add unexpected charges or expose a public endpoint | Cost or unwanted access | Estimate resources before provisioning; use minimal instance/retention settings, access controls, synthetic data, and a documented teardown/ownership plan; keep R2 disabled. |
| Existing branch or workspace state changes during implementation | Lost work or accidental unrelated commit | Re-check branch and status before each phase; preserve owner changes; commit only scoped files; do not force-push; stop if the branch advances unexpectedly. |
| Tests require real credentials or external services | Flaky CI and accidental real actions | Use fakes/fixtures for routine tests; isolate live smoke tests to DEV and dedicated test identities; prevent secrets and personal data from entering logs or artifacts. |

## 7. Acceptance criteria

- Local development starts with documented commands and local/simulated D1 and Queue bindings.
- Remote DEV uses a dedicated Worker/hostname, D1, Queue, OAuth/test applications, and credentials; automated checks reject production resource references.
- Application entry points are thin, and extracted services/adapters have clear responsibilities and typed boundaries.
- Existing route, webhook, OAuth, MCP, queue, idempotency, Feishu/Lark separation, and XiaoT behavior remain compatible, demonstrated by deterministic tests and DEV smoke checks.
- All 14 existing migrations are inventoried; any schema change is additive and backward compatible, with an isolated fresh-install and upgrade validation.
- Python/runtime declarations and the single authoritative lockfile agree; Ruff and pytest checks are documented and enforced in CI.
- Secret source and access path are proven without exposing secret values; `LARK_DOCUMENT_WIKI_TOKEN` is not changed until classified.
- English engineering documentation covers setup, resources, secrets, cost, retention, deployment, and rollback.
- The final review shows no production resource, data, public contract, or user-visible behavior change. Any production deployment is handled separately.

## 8. Rollback and recovery

- Revert a code-only extraction by restoring the preceding Worker version/commit after confirming the prior version remains compatible with current D1 schema and queued messages.
- Keep schema changes additive. Do not rely on destructive reverse migrations; before any schema migration, define an isolated backup/export and recovery procedure appropriate to D1 and verify it before use.
- If DEV isolation, secrets, or smoke checks fail, stop the DEV rollout, restore the prior DEV Worker/configuration, disable the affected test integration, and preserve sanitized diagnostics for review.
- Never use a DEV rollback as authorization to modify production. Production recovery is a separately reviewed operational procedure.

## 9. Decisions required before implementation

1. Confirm that the proposed DEV Worker, hostname, D1, Queue, and test applications are approved, isolated resources, and identify their owner and cost budget.
2. Confirm the approved secret source and whether a short-lived/federated identity can be used by this Cloudflare Worker. If not, approve a documented alternative before changing credential handling.
3. Classify `LARK_DOCUMENT_WIKI_TOKEN` as a secret or non-secret identifier and confirm its authoritative source.
4. Confirm whether the existing DEV deployment mirror on `shuai_review` is the intended implementation base and how it should be reconciled with current `main` before implementation.
5. Agree on the supported Wrangler/Python Worker versions, authoritative lockfile, and which full test suite/checks the owner expects at final review.
6. Confirm D1 retention periods, DEV cleanup cadence, and who can access DEV webhook/OAuth logs.
7. Approve the phase gates and explicitly authorize implementation. Pull request creation and production work require separate explicit direction.
