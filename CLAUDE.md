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
  extraction/        Gemini client, prompts/ (versioned: v1.txt, v2.txt, v3.txt)
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

- `invoices` — id, file_hash (unique), file_path, status (pending/processing/needs_review/approved/extraction_failed/rejected), extracted_json (LLM output, never modified), corrected_json (after human edits, null until first), attempts, processing_started_at, dup_key (indexed), created_at, updated_at. `Invoice.current_json` = corrected if any, else extracted
- `extraction_runs` — invoice_id, raw_response, error, model, prompt_version, latency_ms, input/output tokens, cost_usd (from `app/extraction/pricing.py`; thinking tokens billed as output; unknown model → null, never 0)
- `field_flags` — invoice_id, code (check id, e.g. `total_mismatch`, `duplicate`), field_path (e.g. `line_items[2].unit_price`), reason, severity, resolved, resolved_by, resolved_at, resolution_note
- `corrections` — invoice_id, seq (identity: audit order), field_path, old_value, new_value, corrected_by, note, created_at. A rejection is a row with field_path `status`

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

- Current prompt: `v3` (adds `tax_breakdown`; `v2` added `prices_include_tax`). Old runs keep their version in `extraction_runs.prompt_version`; old `extracted_json` without `tax_breakdown` still validates (defaults to `[]`).
- Main model `gemini-3.5-flash`, fallback `gemini-3.5-flash-lite` (set in `.env` / `app/config.py`). ~3s per invoice.
- `thinking_level="low"`: the default/high thinking took 80–160s for identical output. `minimal` is rejected by these models.
- Newest models (e.g. `gemini-3.8-flash`) were frequently 503-overloaded. `gemini-2.5-*` returns 404 for new keys.
- **Free tier: 20 requests/day for the main model.** Past that every call 429s and the fallback answers. A per-day 429 is non-transient (`client.is_transient`): it skips straight to the fallback, while a per-minute 429 (limit 5) is still retried. `gemini-3.5-flash` is deprecated and Google routes it to `gemini-3.6-flash` (the 429 names 3.6).
- `flash-lite` sometimes returns money as strings with thousands separators (`"1,400.00"`); validation rejects it and the retry usually fixes it.
- `google-genai` is v3.x: verify its API in the container before assuming older docs apply.
- Schema: `app/schemas/extraction.py` → passed as `response_json_schema`. Gemini returns money as JSON numbers; Pydantic turns them into `Decimal`; stored as strings.
- Comparing `Decimal("1858.8") == 1858.8` is **False**. Always compare money to `Decimal("...")`, never float literals.
- Extraction runs in FastAPI `BackgroundTasks` (`app/extraction/service.py`). `POST /invoices/{id}/retry` re-runs failed ones.
- Generate a test invoice: `docker compose exec -T api uv run --frozen python scripts/make_sample_invoice.py storage/samples/x.pdf`

## Eval harness (Phase 5)

```bash
docker compose run --rm -T api uv run --frozen python -m eval.run extract                 # real Gemini, ~38 calls
docker compose run --rm -T api uv run --frozen python -m eval.run extract --only pt-02,syn-019 --model gemini-3.5-flash-lite --fallback-model ""
docker compose run --rm -T api uv run --frozen python -m eval.run score latest            # offline re-score, free
```

- `app/extraction/core.py` `extract_invoice()` holds the retry/fallback/validation logic with no DB. It reports each call to a `RunRecorder`: `service.DbRecorder` writes `extraction_runs`, `eval.run.MemoryRecorder` writes `eval/results/<run_id>/raw/<id>.json` (gitignored). The eval therefore measures the exact production code.
- `extract` freezes raw answers; `score` is pure and re-runnable. Change a metric or a check → re-score the same run, no API cost.
- Scoring (`eval/scoring.py`): money ±0.01, discount by absolute value, dates/currency/bool exact, free text ignores case/spacing/punctuation/accents. `alternatives` accepted per field (not jointly: a mix of two valid readings can score correct while the checks flag it, e.g. pt-03). Line items aligned by position.
- **Headline metric: unsafe approvals** = an invoice with a wrong *critical* field (or a planted problem) and zero flags. `CRITICAL` in `scoring.py` (money, dates, number, vendor name/tax id, currency, line amounts, line count).
- Flags compared by `(code, field_path)` with `run_checks(inv, as_of)`; duplicates simulated in memory in id order. Unexpected flags on a wrong extraction count as useful, on a correct one as false alarms.
- The report shows `Answered by` per model: a run where the fallback answered is not a measurement of the main model.
- First run `20261111-045655-v2` (mixed: flash 15, flash-lite 23): synthetic 100% fields, 0 unsafe; real 97.2% fields, 2 unsafe approvals (pt-02 tax 2.71 vs 3.11: tax isn't checked on tax-included receipts; pt-09 vendor misread), 2 needless reviews (pt-03 mixed reading, pt-05 known check FP). ~$0.003/invoice.

## Eval fixtures (Phase 0)

- `docker compose run --rm -T api uv run --frozen python -m scripts.make_fixtures` rebuilds `eval/fixtures/synthetic/` from the `SPECS` list. It's deterministic (seeded and byte-identical), so never hand-edit those files; change `SPECS` and regenerate.
- Fixture = `<id>.<pdf|png|jpg>` + `<id>.json` containing `expected` (ExtractedInvoice **as printed**, wrong numbers included), `expected_flags` (what `run_checks` must raise at `as_of`), `planted`, and `duplicate_of`.
- Synthetic data is built first and the document is drawn from it, so labels are exact. The generator asserts the checks raise exactly the planted errors and that key values appear in the PDF text.
- Multi-rate synthetic vendors (`pt`, `de_mixed`) print a tax table; single-rate ones show only "VAT 20%: x", so their `tax_breakdown` row has `base: null`. Plant `tax_row` = the pt-02 smudge on paper (syn-031).
- Dates in synthetic invoices use day > 12, so day/month order is never ambiguous.
- `tests/test_fixtures.py` validates every fixture. If you change a check or the schema, regenerate the fixtures.
- Discount `expected` keeps the printed sign (`-107.48` if printed negative); the eval should compare discount by absolute value.
- **Real fixtures** (`eval/fixtures/real/pt-01..10.json`): 10 Portuguese receipts, full labels (line items included), each human-verified (`verified_by`). Images are not in git: run `python -m scripts.fetch_real_fixtures` (downloads from Zenodo once, verifies md5 + per-image sha256).
  - Real labels have **no `tax_breakdown` key yet** (labeled before prompt v3): the eval skips the field and `test_fixtures` tolerates its absence. Adding it needs the usual human review; then drop the tolerance.
  - Real-fixture semantics: `expected_flags` is `[]` (the documents are correct). `check_false_positives` records what our checks wrongly raise on the TRUE data; it's asserted, so it stays visible. Currently only pt-05 (a line-level discount breaks `line_math` and `subtotal_mismatch`).
  - `alternatives` = other acceptable answers per field path (trading vs legal name, "Consumidor final", ambiguous readings). Phase 5 must accept them.
  - `as_of` = invoice date + 7 days, so old receipts don't trigger the 2-year warning.
  - Labeling workflow: draft from the photo (never from LLM output), cross-check with the dataset labels and with the receipt's own arithmetic, then human review with `python -m scripts.prepare_label_review` (page served by `.claude/launch.json` "label-review"). The dataset's own labels get tax wrong on every multi-rate receipt (they record only one rate), and a smudged digit fooled three readers on pt-02. Settle disputes with arithmetic.
  - Don't put private individuals' data in labels (a dental receipt with a patient's name was dropped).
- Real invoices: Zenodo 7213544 (CC BY 4.0, attribution required) is downloaded and checksum-verified in gitignored `storage/datasets/zenodo-7213544/`. It has 190 phone photos of Portuguese till receipts (tax included). Its labels cover header fields only, as printed (6 date formats, `12,20` vs `9.98`, lowercase). Most receipts show a private buyer NIF (`121653897`), so **don't commit the images**: commit our checked label JSON plus a fetch script. Hand-label from the document, not from LLM output.
- Other candidates: katanaml invoices-donut-data-v1 (MIT, likely synthetic), RealKIE FCC invoices (real, CC BY-NC), DocILE (token-gated, license unclear). Ask before downloading.

## Reliability design (decided, don't undo)

- **No lost jobs:** `process_invoice` first *claims* the invoice with one atomic `UPDATE ... WHERE status='pending'` (→ `processing`, `attempts+1`). Only one worker can win, so nothing runs twice.
- On server startup, `recover_stuck_jobs` re-queues everything left in `pending`/`processing`. This **assumes a single API instance**; with several instances it must become a real job queue (Phase 6).
- Poison files: after `MAX_JOB_ATTEMPTS` (3) worker pickups → `extraction_failed`. `POST /invoices/{id}/retry` resets `attempts` to 0.
- **Business duplicates** (same invoice, different file): `app/checks/duplicates.py` builds `dup_key` = normalized vendor + invoice number, stored in an indexed column. A match adds an `error` flag (never a silent reject); a human decides.
- Known limits: a vendor name that differs beyond legal suffixes (Ltd/Inc/...) won't match. Two copies processed at the exact same moment may miss each other.
- **Approval rule:** `approved` only when an extraction has zero flags; *any* flag (warning or error) → `needs_review`. Loosen only with eval data (Phase 5).
- Checks live in `app/checks/` (`required`, `arithmetic`, `dates`, `tax`), are collected by `runner.run_checks(inv, today)`, and return `checks.base.Flag`. `today` is a parameter so checks stay pure. Duplicate detection needs the DB, so it runs in `service.py`, not the runner.
- Total check accepts the discount printed as positive or negative, and the subtotal given before or after the discount; it flags only when none of these readings adds up. Line math rounds half-up to cents.
- **Tax breakdown** (`app/checks/tax.py`): `tax_breakdown` = one row per tax rate (`rate` in percent, `base`, `amount`; null when not printed). `tax_line_math`: base × rate ≠ row amount. `tax_breakdown_mismatch`: rows don't sum to `tax`. `tax_base_mismatch`: bases + tax ≠ total (holds for tax added and included). Each runs only when its numbers are present. Added because pt-02's smudged 23% row (2,96 read as 2,56) passed every other check.
- **Tax added vs included** varies by document type, not just country (US and B2B invoices add tax; EU shop receipts include it). The extracted field `prices_include_tax` decides: `false` → total = base − discount + tax; `true` → total = base − discount; `null` → accept either. Never infer it from the country. On tax-included receipts the tax base ("Base Imp.") is not the subtotal.
- **Review (Phase 4, `app/review/`):** `PATCH /invoices/{id}` applies all changes or none. The result is validated against `ExtractedInvoice`, each changed field appends a `corrections` row, the checks re-run, and status is recomputed (no open flags → approved). `POST .../flags/{flag_id}/resolve` = a human accepts a flagged value. `POST .../reject` is final. `GET .../corrections` returns the audit trail.
- On re-check, a flag's identity is `(code, field_path, reason)`. Problem gone → resolved by `system:recheck` (never deleted). A problem a human already resolved is not raised again. Changing vendor or invoice number re-runs duplicate detection.
- Corrections compare values as Decimal/date, so `"22"` vs `"22.00"` is not a change. Invoice rows are locked (`FOR UPDATE`) during review actions. Order the audit trail by `seq`, not `created_at`: `now()` is the transaction start, so all rows from one PATCH share a timestamp.
- No auth yet: reviewers name themselves in the request body (`corrected_by`).
- Adding a NOT NULL column to a table with rows: add it nullable, backfill, then `alter_column(nullable=False)`.
- Alembic autogenerate does **not** detect new Postgres enum values: add `ALTER TYPE ... ADD VALUE` by hand.
- A new derived column needs a **backfill** for old rows (see `scripts/backfill_dup_keys.py`, run with `python -m scripts.<name>`). Backfills must be idempotent.
- Tests set `RECOVER_ON_STARTUP=false` (in `tests/conftest.py`) so startup never touches the dev DB or real Gemini.

## Scope discipline

Build in phase order. Don't add queues, auth, S3, or a frontend until the eval harness (Phase 5) works.

| Phase | Goal | Status |
|---|---|---|
| 0 | Ground-truth fixtures (20–30 labeled invoices) | done (31 synthetic + 10 real, human-verified) |
| 1 | Skeleton: FastAPI + Postgres + Alembic + upload/get, idempotent hashing | done |
| 2 | Gemini extraction + Pydantic validation + retry + raw storage | done |
| 3 | Deterministic checks + flags + status | done |
| 4 | Review endpoints: list flagged, PATCH corrections w/ audit | done |
| 5 | Eval harness: field accuracy, flag precision/recall, cost/latency | done (harness + first run; main-model numbers blocked by free-tier quota) |
| 6 | Hardening: logging, Dockerfile, README with real eval numbers | todo |

Update the Status column when a phase is done.

## Working style

- Small, reviewable changes; run the relevant tests after each change.
- When unsure about a design choice that affects the data model or API contract, ask before implementing.
- The user is learning — briefly explain *why* when introducing a new pattern or tool.
