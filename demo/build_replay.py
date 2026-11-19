"""Build the demo's replay scenarios from a frozen eval run.

    docker compose run --rm -T api uv run --frozen python -m demo.build_replay [--run 20261111-061513-v3]

For each entry in SCENARIOS it copies the fixture file to app/demo/scenarios/files/ and writes
app/demo/scenarios/<name>.json with every call flash-lite made for it (errors included) plus what must happen
when it is replayed: `expected` = what the CURRENT checks make of the recorded answer. The builder refuses
to write a scenario unless:

- the run used the current prompt file and response schema (else the recording answers another question),
- the checks raise exactly the fixture's planted problems (plus `duplicate` when it depends on its original).

The eval run's raw answers are gitignored (eval/results/), so the scenarios are the committed copy. Change a
check, the prompt or the schema -> tests/test_demo_replay.py fails until this is re-run (re-read the blurbs).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from dataclasses import dataclass, field
from typing import Any

from app.checks.duplicates import DUPLICATE_CODE
from app.checks.runner import run_checks
from app.extraction.client import is_transient
from app.extraction.core import load_prompt, schema_fingerprint
from app.demo.replay import FILES_DIR, SCENARIOS_DIR, sha256
from app.schemas.extraction import ExtractedInvoice
from eval.dataset import RESULTS_DIR, load_fixtures

DEFAULT_RUN = "20261111-061513-v3"  # flash-lite, prompt v3, no fallback: every synthetic invoice read correctly

CLEAN = "Clean invoices"
CAUGHT = "Planted mistakes"
RELIABILITY = "When Gemini fails"


@dataclass(frozen=True)
class Spec:
    fixture: str
    group: str
    title: str
    blurb: str
    depends_on: list[str] = field(default_factory=list)


SCENARIOS = [
    Spec("syn-001", CLEAN, "UK software invoice (PDF)",
         "A clean PDF. Every check passes (line math, subtotal, VAT, total, dates, the UK VAT number's check "
         "digits), so it is approved with no human in the loop."),
    Spec("syn-011", CLEAN, "German invoice, scanned (JPG)",
         "A slightly blurred, tilted scan. Gemini reads the image; the same checks verify every number it "
         "returned, in code."),
    Spec("syn-004", CLEAN, "Indian invoice in rupees",
         "Rupees and 18% GST. The vendor's GSTIN (Indian tax ID) is verified by its check digit."),
    Spec("syn-029", CLEAN, "Portuguese receipt, prices include VAT",
         "Two VAT rates, tax already inside the prices. The model reports prices_include_tax, and the total "
         "check adds up the receipt the way it was printed."),
    Spec("syn-019", CAUGHT, "Total doesn't add up",
         "The printed total is wrong. Gemini copies it faithfully (that's its job); the total check recomputes "
         "subtotal + tax and sends the invoice to review instead of approving it."),
    Spec("syn-017", CAUGHT, "Line math is off",
         "One line's amount isn't quantity x unit price. The check points at the exact line."),
    Spec("syn-018", CAUGHT, "Subtotal doesn't match the lines",
         "The printed subtotal differs from the sum of the line amounts."),
    Spec("syn-020", CAUGHT, "Due before it was issued",
         "The due date is earlier than the invoice date: usually a misread day or month."),
    Spec("syn-021", CAUGHT, "Invoice date years ago",
         "An invoice dated more than two years back usually means a misread year. A warning, still a review."),
    Spec("syn-022", CAUGHT, "No invoice number",
         "The document has no invoice number. The model returns null instead of inventing one, and the "
         "required-field check flags it."),
    Spec("syn-031", CAUGHT, "One VAT row is wrong",
         "One row of the VAT table doesn't equal base x rate, and the bases plus tax no longer add up to the "
         "total. Both tax checks fire."),
    Spec("syn-025", CAUGHT, "Same invoice, sent twice",
         "A re-sent copy of the UK invoice as a scan (a different file, so the upload isn't deduplicated by "
         "hash). Same vendor + invoice number -> flagged as a possible duplicate for a human to decide. Replays "
         "the original first if your sandbox doesn't have it yet.",
         depends_on=["syn-001"]),
    Spec("syn-015", RELIABILITY, "Rate-limited, then retried",
         "The first call hit Gemini's per-minute limit (429). A per-minute limit is transient, so the pipeline "
         "retried with backoff and the second call answered. Both calls are stored."),
]

_HTTP_CODE = re.compile(r"^(\d{3}):")


def build(run_id: str) -> None:
    run_dir = RESULTS_DIR / run_id
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest["schema_sha"] != schema_fingerprint():
        raise SystemExit(f"run {run_id} used schema {manifest['schema_sha']}, current is {schema_fingerprint()}")
    prompt = load_prompt(manifest["prompt_version"])
    fixtures = {f.id: f for f in load_fixtures({s.fixture for s in SCENARIOS})}

    for old in SCENARIOS_DIR.glob("*.json"):
        old.unlink()
    shutil.rmtree(FILES_DIR, ignore_errors=True)
    FILES_DIR.mkdir(parents=True)

    for order, spec in enumerate(SCENARIOS):
        fx = fixtures[spec.fixture]
        raw = json.loads((run_dir / "raw" / f"{spec.fixture}.json").read_text(encoding="utf-8"))
        if raw["extracted"] is None:
            raise SystemExit(f"{spec.fixture}: extraction failed in run {run_id}")
        data = fx.file_path.read_bytes()
        file_name = fx.file_path.name
        shutil.copyfile(fx.file_path, FILES_DIR / file_name)

        flags = run_checks(ExtractedInvoice.model_validate(raw["extracted"]), fx.as_of)
        keys = sorted({(f.code, f.field_path) for f in flags})
        if fx.duplicate_of:
            keys = sorted({*keys, (DUPLICATE_CODE, "invoice_number")})
            planted = {*fx.expected_flags, (DUPLICATE_CODE, "invoice_number")}
        else:
            planted = set(fx.expected_flags)
        if set(keys) != planted:
            raise SystemExit(f"{spec.fixture}: checks raise {keys}, fixture plants {sorted(planted)}")

        scenario: dict[str, Any] = {
            "name": spec.fixture, "order": order, "group": spec.group, "title": spec.title, "blurb": spec.blurb,
            "fixture": spec.fixture, "file": file_name, "content_type": fx.mime_type, "as_of": fx.as_of.isoformat(),
            "depends_on": spec.depends_on,
            "recorded": {
                "run_id": run_id, "model": manifest["model"], "prompt_version": manifest["prompt_version"],
                "prompt_sha256": sha256(prompt.encode()), "prompt_chars": len(prompt),
                "schema_sha": manifest["schema_sha"], "file_sha256": sha256(data),
                "calls": [_call(a) for a in raw["attempts"]],
            },
            "expected": {
                "status": "needs_review" if keys else "approved",
                "flags": [list(k) for k in keys],
                "flag_count": len(flags) + (1 if fx.duplicate_of else 0),
            },
        }
        (SCENARIOS_DIR / f"{spec.fixture}.json").write_text(
            json.dumps(scenario, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"{spec.fixture}: {len(raw['attempts'])} call(s), {scenario['expected']['status']}, "
              + (", ".join(f"{f.code} {f.field_path}: {f.reason}" for f in flags) or "no flags"))


def _call(a: dict[str, Any]) -> dict[str, Any]:
    error = a["error"]
    transient = False
    if error and a["raw_response"] is None and (m := _HTTP_CODE.match(error)):
        transient = is_transient(int(m.group(1)), error)
    return {
        "raw_response": a["raw_response"], "error": error, "transient": transient,
        "latency_ms": a["latency_ms"], "input_tokens": a["input_tokens"], "output_tokens": a["output_tokens"],
        "thinking_tokens": a.get("thinking_tokens"),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", default=DEFAULT_RUN)
    build(parser.parse_args().run)
