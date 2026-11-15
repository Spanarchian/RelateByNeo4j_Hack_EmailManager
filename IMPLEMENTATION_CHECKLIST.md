# Implementation checklist

Legend: ✅ implemented and verified · 🔑 implemented, awaiting external credentials for live verification

## Phase 1 — Inspection
- ✅ Workspace inspected (empty apart from README); Python 3.14.5, uv 0.12.19, Docker 29.9.3 available
- ✅ Relate researched: **Relate by Neo4j** (relatedb.ai, launched 2026-11-11) is an embedded Cypher DB; official Python client `relate-db` 1.0.1. The unrelated, deprecated `@relate/cli` (Neo4j Desktop's Node.js DBMS manager) was ruled out.
- ✅ Relate behaviour probed locally: constraints, MERGE, transactions, single writer ("storage locked"), thread-bound connections, map properties rejected
- ✅ Gmail `users.messages.send` and Mailchimp Transactional `messages/send.json` contracts checked; real unauthenticated error responses observed (Mandrill `Invalid_Key` → HTTP 401)

## Phase 2 — Foundation
- ✅ `pyproject.toml`, `uv.lock`, package layout, pydantic-settings configuration, FastAPI app factory

## Phase 3 — Graph
- ✅ Relate adapter (single connection on a dedicated thread, transactional retry)
- ✅ Schema: 10 labels, 9 relationship types, 12 uniqueness constraints, stable uuid5 ids
- ✅ Repository and parameterised queries (pending approvals, volunteer/employee onboarding, unsent, failed, history)
- ✅ Idempotent `seed_graph.py`, read-only `check_graph.py`
- ✅ Persistence across reopen and container restart verified

## Phase 4 — Providers
- 🔑 Gmail provider (OAuth, refresh, MIME, message ids) — tested with mocked HTTP
- 🔑 Mailchimp Transactional provider — tested with mocked HTTP
- ✅ Shared `EmailProvider` interface, three-valued outcome, provider service with retry and safe failover

## Phase 5 — Onboarding
- ✅ Four templates (plain text and HTML), sandboxed renderer, volunteer and employee flows, draft preparation

## Phase 6 — Agents and approval
- ✅ Onboarding agent, deterministic review agent (sender, recipients, identity, dates, placeholders, content, sensitive data)
- ✅ Human approval bound to a content hash; edit invalidation; send-time re-verification; four-eyes; audit history

## Phase 7 — API
- ✅ All required endpoints plus `PATCH /emails/{id}`, `/cancel`, `/reconcile`, `/unsent`, `/failed-deliveries`, `GET /onboarding?type=`
- ✅ API key authentication (hashed keys), roles, rate limiting, structured errors, OpenAPI

## Phase 8 — Testing
- ✅ 221 tests pass (163 unit, 38 integration, 20 E2E); 2 external tests skipped (opt-in)
- ✅ Coverage 96.7% (branch coverage; gate 85%)

## Phase 9 — Documentation
- ✅ README, `docs/SAD.md`, 12 Sphinx pages, `.readthedocs.yaml`; `sphinx-build -W` passes

## Phase 10 — Docker
- ✅ Image builds; container healthy, non-root (uid 10001), read-only root filesystem
- ✅ Data persists across restart; a second writer is refused; token provisioning verified

## Phase 11 — Final validation
- ✅ `uv sync --all-groups`, `ruff check`, `ruff format --check`, `mypy src` (strict), `pytest`, `task docs`, `docker compose config`
- ✅ CI workflow steps simulated locally
- ✅ Every Taskipy task invoked (`auth` exits 2 with instructions while no OAuth client file exists)
- ✅ Distribution ZIP created
