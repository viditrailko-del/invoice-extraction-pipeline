"""Eval CLI.

    python -m eval.run extract [--only syn-001,pt-05] [--model M] [--prompt-version v2]
        Calls REAL Gemini once per fixture through the same extract_invoice() the API
        uses, saves each result to eval/results/<run_id>/, then scores the run.
    python -m eval.run score <run_id | latest>
        Re-scores saved results. Offline and free: tweak a metric or a check and re-run.

Why two steps: the LLM call is the only slow, paid, non-deterministic part. Freezing its
output means scoring is reproducible, and two runs (e.g. prompt v2 vs v3) can be
compared on exactly the same answers.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.checks.duplicates import DUPLICATE_CODE, make_dup_key
from app.config import Settings, get_settings
from app.extraction.client import Extractor, GeminiExtractor
from app.extraction.core import Attempt, extract_invoice, response_schema
from app.schemas.extraction import ExtractedInvoice
from eval.dataset import RESULTS_DIR, Fixture, load_fixtures
from eval.report import FixtureScore, build_report, render_markdown
from eval.scoring import decide, predicted_flags, score_fields, score_flags


class MemoryRecorder:
    """The eval's RunRecorder: keeps attempts in memory instead of the DB."""

    def __init__(self) -> None:
        self.attempts: list[Attempt] = []

    def record(self, attempt: Attempt) -> None:
        self.attempts.append(attempt)

    def update(self, attempt: Attempt) -> None:
        pass  # same object, already in the list


# --- extract (calls the API) ----------------------------------------------------


def extract_one(fx: Fixture, extractor: Extractor, settings: Settings) -> dict[str, Any]:
    recorder = MemoryRecorder()
    start = time.monotonic()
    crash = None
    try:
        extracted = extract_invoice(
            fx.file_path.read_bytes(), fx.mime_type, extractor, settings, recorder
        )
    except Exception as e:  # same rule as the API: a bug becomes "failed", not a crash
        extracted, crash = None, f"{type(e).__name__}: {e}"
    return {
        "fixture_id": fx.id,
        "split": fx.split,
        "extracted": extracted.model_dump(mode="json") if extracted else None,
        "crash": crash,
        "wall_ms": int((time.monotonic() - start) * 1000),
        "attempts": [asdict(a) for a in recorder.attempts],
    }


def cmd_extract(args: argparse.Namespace) -> Path:
    overrides = {
        k: v
        for k, v in {
            "gemini_model": args.model,
            "gemini_fallback_model": args.fallback_model,
            "prompt_version": args.prompt_version,
        }.items()
        if v is not None
    }
    settings = get_settings().model_copy(update=overrides)
    fixtures = load_fixtures(set(args.only.split(",")) if args.only else None)
    missing = [fx.id for fx in fixtures if not fx.file_path.exists()]
    if missing:
        print(f"skipping {len(missing)} fixture(s) with no file: {', '.join(missing)}")
        print("  (real receipts: run `python -m scripts.fetch_real_fixtures` first)")
        fixtures = [fx for fx in fixtures if fx.file_path.exists()]

    extractor = GeminiExtractor(
        settings.gemini_api_key,
        timeout_s=settings.gemini_timeout_s,
        thinking_level=settings.gemini_thinking_level,
    )
    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + f"-{settings.prompt_version}"
    run_dir = RESULTS_DIR / run_id
    run_dir.mkdir(parents=True)
    manifest = {
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "model": settings.gemini_model,
        "fallback_model": settings.gemini_fallback_model,
        "prompt_version": settings.prompt_version,
        "thinking_level": settings.gemini_thinking_level,
        # The response schema shapes answers as much as the prompt does (e.g. number-only
        # money): fingerprint it so runs from before/after a schema change can be told apart.
        "schema_sha": schema_fingerprint(),
        "git_commit": _git_commit(),
        "fixtures": [fx.id for fx in fixtures],
    }
    _write_json(run_dir / "manifest.json", manifest)

    for i, fx in enumerate(fixtures, 1):
        result = extract_one(fx, extractor, settings)
        _write_json(run_dir / "raw" / f"{fx.id}.json", result)
        status = "ok" if result["extracted"] else "FAILED"
        print(f"[{i}/{len(fixtures)}] {fx.id}: {status} "
              f"({len(result['attempts'])} call(s), {result['wall_ms']} ms)", flush=True)
    return run_dir


# --- score (offline) -------------------------------------------------------------


def score_run(run_dir: Path) -> tuple[dict[str, Any], list[FixtureScore]]:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    fixtures = {fx.id: fx for fx in load_fixtures(set(manifest["fixtures"]))}
    seen_dup_keys: dict[str, str] = {}
    scores = []
    for fx_id in sorted(manifest["fixtures"]):  # originals before re-scans
        fx = fixtures[fx_id]
        raw = json.loads((run_dir / "raw" / f"{fx_id}.json").read_text(encoding="utf-8"))
        extracted_json = raw["extracted"]
        fields = score_fields(fx.expected, extracted_json, fx.alternatives)
        extraction_wrong = any(not f.ok and f.critical for f in fields)

        predicted: set[tuple[str, str]] = set()
        if extracted_json is not None:
            extracted = ExtractedInvoice.model_validate(extracted_json)
            predicted = predicted_flags(extracted, fx.as_of)
            # Duplicate detection is a DB query in the API; here, an in-memory equivalent
            # over the invoices "uploaded" so far in this run.
            key = make_dup_key(extracted)
            if key and key in seen_dup_keys:
                predicted.add((DUPLICATE_CODE, "invoice_number"))
            elif key:
                seen_dup_keys[key] = fx_id

        attempts = raw["attempts"]
        costs = [Decimal(a["cost_usd"]) for a in attempts if a["cost_usd"] is not None]
        ok_calls = [a for a in attempts if a["raw_response"] is not None]
        scores.append(
            FixtureScore(
                id=fx_id,
                split=fx.split,
                failed=extracted_json is None,
                fields=fields,
                flags=score_flags(fx.expected_flags, predicted, extraction_wrong),
                predicted=predicted,
                decision=decide(
                    should_review=bool(fx.expected_flags) or extraction_wrong,
                    flagged=extracted_json is None or bool(predicted),
                ),
                known_check_fps=fx.known_check_false_positives,
                calls=len(attempts),
                latency_ms=sum(a["latency_ms"] or 0 for a in attempts),
                # cost is only "known" if every successful call had a price
                cost_usd=sum(costs, Decimal(0)) if ok_calls and len(costs) == len(ok_calls) else None,
                input_tokens=sum(a["input_tokens"] or 0 for a in attempts),
                output_tokens=sum((a["output_tokens"] or 0) + (a["thinking_tokens"] or 0) for a in attempts),
                answered_by=_answered_by(attempts) if extracted_json is not None else None,
            )
        )
    return manifest, scores


def cmd_score(run_dir: Path) -> None:
    manifest, scores = score_run(run_dir)
    report = build_report(scores)
    _write_json(run_dir / "report.json", report)
    md = render_markdown(manifest, report, scores)
    (run_dir / "report.md").write_text(md, encoding="utf-8")
    print()
    print(md.split("\n## ")[0])  # the summary table
    print(f"full report: {run_dir / 'report.md'}")


# --- helpers ---------------------------------------------------------------------


def _resolve_run(name: str) -> Path:
    if name == "latest":
        runs = sorted(p for p in RESULTS_DIR.glob("*") if (p / "manifest.json").exists())
        if not runs:
            sys.exit("no eval runs yet: run `python -m eval.run extract` first")
        return runs[-1]
    return RESULTS_DIR / name


def _answered_by(attempts: list[dict[str, Any]]) -> str | None:
    """The last call that returned output without a validation error is the one used."""
    used = [a for a in attempts if a["raw_response"] is not None and not a["error"]]
    return used[-1]["model"] if used else None


def schema_fingerprint() -> str:
    canonical = json.dumps(response_schema(), sort_keys=True).encode()
    return hashlib.sha256(canonical).hexdigest()[:12]


def _git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m eval.run")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("extract", help="call Gemini on every fixture, then score")
    ex.add_argument("--only", help="comma-separated fixture ids")
    ex.add_argument("--model")
    ex.add_argument("--fallback-model")
    ex.add_argument("--prompt-version")
    sc = sub.add_parser("score", help="re-score a saved run (offline)")
    sc.add_argument("run", nargs="?", default="latest")
    args = parser.parse_args()

    if args.cmd == "extract":
        cmd_score(cmd_extract(args))
    else:
        cmd_score(_resolve_run(args.run))


if __name__ == "__main__":
    main()
