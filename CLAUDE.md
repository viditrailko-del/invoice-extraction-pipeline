# Invoice Extraction Pipeline

Backend service: invoice PDFs/images → Gemini structured extraction → Pydantic validation → deterministic checks → Postgres → human review → eval harness.

**The differentiator is the reliability layer around the LLM call, not the call itself.** Every design choice should make extraction measurable, auditable, and safe to be wrong.

## Flow

```
Upload (POST /invoices)
  → store file + SHA-256 hash (idempotent: same file → same record)
  → background task claims it (status pending → processing)
  → Gemini structured output (raw response saved first)
  → Pydantic validation (1 retry with the validation error fed back)
  → deterministic checks → field_flags
  → status = approved | needs_review | extraction_failed
  → GET /invoices?status=needs_review → PATCH corrections → re-run checks
  → eval harness measures accuracy + flag precision/recall
```

## Stack

- Python 3.12, `uv` for deps/venv
- FastAPI, Pydantic v2
- SQLAlchemy 2.0 (typed `Mapped[]` style) + Alembic migrations
- Postgres 16 via `docker-compose`
- `google-genai` SDK (Gemini)
- pytest

## Commands

**Everything runs inside Docker.** This Windows machine has Smart App Control, which blocks the compiled DLLs in pydantic-core/SQLAlchemy/psycopg, so a local `.venv` cannot run the app. Do not try to fix that locally; use the container.

```bash
docker compose up -d --wait                                               # start Postgres + API (http://localhost:8000/docs)
docker compose logs -f api                                                # API logs
docker compose run --rm -T api uv run --frozen pytest -q                  # all tests
docker compose run --rm -T api uv run --frozen alembic revision --autogenerate -m "msg"   # new migration (review the file!)
docker compose run --rm -T api uv run --frozen alembic upgrade head       # apply migrations
docker compose exec -T db psql -U invoice -d invoices -c "select ..."     # inspect DB
docker compose build api                                                  # after changing dependencies
python -m uv add <pkg> && python -m uv lock                               # add a dependency (then rebuild)
```

- Source is bind-mounted into the container, so code edits auto-reload; only dependency changes need a rebuild.
- The container's venv lives at `/opt/venv`, separate from the mounted source.
- Inside Docker the DB host is `db`; from Windows it's `localhost:5432`.
- Tests use the separate `invoices_test` DB (created by `docker/init-test-db.sql`) and build the schema with `create_all`. The dev DB uses Alembic.
- `uv` is installed via pip and isn't on PATH: use `python -m uv`.

Secrets live in `.env` (never committed); `.env.example` lists the keys (`GEMINI_API_KEY`, `DATABASE_URL`).

## Layout (target)

```
app/
  main.py            FastAPI app + routers
  api/               route handlers (thin — no business logic)
  schemas/           Pydantic models: extraction.py = ExtractedInvoice, LineItem, Vendor (source of truth);
                     invoice.py = API response models
  extraction/        Gemini client, prompts/ (versioned: v1.txt, v2.txt)
  checks/            deterministic check functions (pure)
  db/                SQLAlchemy models, session
alembic/             migrations
scripts/             one-off helpers (sample invoice generator, backfills); run with python -m scripts.<name>
eval/
  fixtures/          invoice files + hand-labeled expected JSON
  run.py             eval CLI
tests/
```

## Data model

- `invoices` — id, file_hash (unique), file_path, status (pending/processing/needs_review/approved/extraction_failed), extracted_json, attempts, processing_started_at, dup_key (indexed), created_at, updated_at
- `extraction_runs` — invoice_id, raw_response, error, model, prompt_version, latency_ms, input/output tokens, cost_usd (column exists but is **not computed yet**; planned for Phase 5)
- `field_flags` — invoice_id, field_path (e.g. `line_items[2].unit_price`), reason, severity, resolved
- `corrections` — invoice_id, field_path, old_value, new_value, corrected_by, created_at

## Rules (important)

1. **Never trust LLM math.** Totals, sums, tax, dates are verified in `checks/`, in code.
2. **Never overwrite history.** Raw LLM output is stored before parsing; corrections are appended to `corrections`, not silently written over extracted data.
3. **Checks are pure functions**: `(Invoice) -> list[Flag]`. No DB, no network. Each new check gets unit tests (passing case + failing case + edge case).
4. **Money is `Decimal`, never `float`.** Compare with a rounding tolerance (e.g. 0.01).
5. **The Pydantic schema is the single source of truth** — it generates the Gemini response schema. Don't hand-write a second schema.
6. **Prompts are versioned files.** Changing a prompt = new version; record `prompt_version` on every run.
7. **Tests never call real Gemini.** Mock the extraction client; only the eval harness hits the real API.
8. LLM failures must degrade to `extraction_failed`, never a 500 or crash.
9. Route handlers stay thin; logic lives in `extraction/` and `checks/`.
10. Schema changes go through an Alembic migration — never edit the DB by hand.

## Extraction notes (learned the hard way)

- Main model `gemini-3.5-flash`, fallback `gemini-3.5-flash-lite` (set in `.env` / `app/config.py`). ~3s per invoice.
- `thinking_level="low"`: the default/high thinking took 80–160s for identical output. `minimal` is rejected by these models.
- Newest models (e.g. `gemini-3.8-flash`) were frequently 503-overloaded. `gemini-2.5-*` returns 404 for new keys.
- `google-genai` is v3.x: verify its API in the container before assuming older docs apply.
- Schema: `app/schemas/extraction.py` → passed as `response_json_schema`. Gemini returns money as JSON numbers; Pydantic turns them into `Decimal`; stored as strings.
- Comparing `Decimal("1858.8") == 1858.8` is **False**. Always compare money to `Decimal("...")`, never float literals.
- Extraction runs in FastAPI `BackgroundTasks` (`app/extraction/service.py`). `POST /invoices/{id}/retry` re-runs failed ones.
- Generate a test invoice: `docker compose exec -T api uv run --frozen python scripts/make_sample_invoice.py storage/samples/x.pdf`

## Reliability design (decided, don't undo)

- **No lost jobs:** `process_invoice` first *claims* the invoice with one atomic `UPDATE ... WHERE status='pending'` (→ `processing`, `attempts+1`). Only one worker can win, so nothing runs twice.
- On server startup, `recover_stuck_jobs` re-queues everything left in `pending`/`processing`. This **assumes a single API instance**; with several instances it must become a real job queue (Phase 6).
- Poison files: after `MAX_JOB_ATTEMPTS` (3) worker pickups → `extraction_failed`. `POST /invoices/{id}/retry` resets `attempts` to 0.
- **Business duplicates** (same invoice, different file): `app/checks/duplicates.py` builds `dup_key` = normalized vendor + invoice number, stored in an indexed column. A match adds an `error` flag (never a silent reject); a human decides.
- Known limits: a vendor name that differs beyond legal suffixes (Ltd/Inc/...) won't match. Two copies processed at the exact same moment may miss each other.
- **Approval rule:** `approved` only when an extraction has zero flags; *any* flag (warning or error) → `needs_review`. Loosen only with eval data (Phase 5).
- Checks live in `app/checks/` (`required`, `arithmetic`, `dates`), are collected by `runner.run_checks(inv, today)`, and return `checks.base.Flag`. `today` is a parameter so checks stay pure. Duplicate detection needs the DB, so it runs in `service.py`, not the runner.
- Total check accepts the discount printed as positive or negative, and the subtotal given before or after the discount; it flags only when none of these readings adds up. Line math rounds half-up to cents.
- Alembic autogenerate does **not** detect new Postgres enum values: add `ALTER TYPE ... ADD VALUE` by hand.
- A new derived column needs a **backfill** for old rows (see `scripts/backfill_dup_keys.py`, run with `python -m scripts.<name>`). Backfills must be idempotent.
- Tests set `RECOVER_ON_STARTUP=false` (in `tests/conftest.py`) so startup never touches the dev DB or real Gemini.

## Scope discipline

Build in phase order. Don't add queues, auth, S3, or a frontend until the eval harness (Phase 5) works.

| Phase | Goal | Status |
|---|---|---|
| 0 | Ground-truth fixtures (20–30 labeled invoices) | todo |
| 1 | Skeleton: FastAPI + Postgres + Alembic + upload/get, idempotent hashing | done |
| 2 | Gemini extraction + Pydantic validation + retry + raw storage | done |
| 3 | Deterministic checks + flags + status | done |
| 4 | Review endpoints: list flagged, PATCH corrections w/ audit | todo |
| 5 | Eval harness: field accuracy, flag precision/recall, cost/latency | todo |
| 6 | Hardening: logging, Dockerfile, README with real eval numbers | todo |

Update the Status column when a phase is done.

## Working style

- Small, reviewable changes; run the relevant tests after each change.
- When unsure about a design choice that affects the data model or API contract, ask before implementing.
- The user is learning — briefly explain *why* when introducing a new pattern or tool.
