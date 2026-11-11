"""Turn per-fixture scores into summary numbers and a markdown report. Pure."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from eval.dataset import FlagKey
from eval.scoring import CRITICAL, Decision, FieldResult, FlagResult


@dataclass
class FixtureScore:
    id: str
    split: str
    failed: bool
    fields: list[FieldResult]
    flags: FlagResult
    predicted: set[FlagKey]
    decision: Decision
    known_check_fps: set[FlagKey] = field(default_factory=set)
    calls: int = 0
    latency_ms: int = 0  # sum over every LLM call for this invoice, retries included
    cost_usd: Decimal | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    answered_by: str | None = None  # model whose answer was used (None: extraction failed)

    @property
    def wrong_fields(self) -> list[FieldResult]:
        return [f for f in self.fields if not f.ok]

    @property
    def critical_wrong(self) -> list[FieldResult]:
        return [f for f in self.wrong_fields if f.critical]


def _pct(num: int, den: int) -> float | None:
    return round(100 * num / den, 1) if den else None


def _percentile(values: list[int], p: float) -> int | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, round(p / 100 * (len(s) - 1)))]


def summarize(scores: list[FixtureScore]) -> dict[str, Any]:
    fields = [f for s in scores for f in s.fields]
    by_field: dict[str, Counter[str]] = defaultdict(Counter)
    for f in fields:
        by_field[f.field][f.outcome] += 1

    matched = sum(len(s.flags.matched) for s in scores)
    missed = sum(len(s.flags.missed) for s in scores)
    on_bad = sum(len(s.flags.on_bad_extraction) for s in scores)
    false_alarms = sum(len(s.flags.false_alarms) for s in scores)
    predicted = sum(len(s.predicted) for s in scores)

    decisions = Counter(s.decision for s in scores)
    costs = [s.cost_usd for s in scores if s.cost_usd is not None]
    latencies = [s.latency_ms for s in scores if s.calls]
    n = len(scores)

    return {
        "invoices": n,
        "extraction_failed": sum(s.failed for s in scores),
        "fields": {
            "total": len(fields),
            "accuracy_pct": _pct(sum(f.ok for f in fields), len(fields)),
            "critical_accuracy_pct": _pct(
                sum(f.ok for f in fields if f.critical), sum(f.critical for f in fields)
            ),
            "invoices_all_correct": sum(not s.wrong_fields for s in scores),
            "invoices_critical_correct": sum(not s.critical_wrong for s in scores),
            "outcomes": dict(Counter(f.outcome for f in fields)),
            "by_field": {
                name: {
                    "accuracy_pct": _pct(c["correct"], sum(c.values())),
                    **dict(c),
                }
                for name, c in sorted(by_field.items())
            },
        },
        "flags": {
            "expected": matched + missed,
            "predicted": predicted,
            "matched": matched,
            "missed": missed,
            "on_bad_extraction": on_bad,
            "false_alarms": false_alarms,
            # Recall: of the problems planted in the documents, how many did we flag?
            "recall_pct": _pct(matched, matched + missed),
            # Precision counts flags that caught an extraction error as useful too.
            "precision_pct": _pct(matched + on_bad, predicted),
        },
        # The fallback model may have answered some invoices: say so, or the numbers lie.
        "answered_by": dict(Counter(s.answered_by or "none (failed)" for s in scores)),
        "decisions": {d: decisions.get(d, 0) for d in Decision.__args__},  # type: ignore[attr-defined]
        "unsafe_approvals": [s.id for s in scores if s.decision == "unsafe_approval"],
        "cost_latency": {
            "llm_calls": sum(s.calls for s in scores),
            "total_cost_usd": str(sum(costs, Decimal(0))) if costs else None,
            "mean_cost_usd": str((sum(costs, Decimal(0)) / len(costs)).quantize(Decimal("0.000001")))
            if costs
            else None,
            "cost_known_for": f"{len(costs)}/{n}",
            "latency_ms_p50": _percentile(latencies, 50),
            "latency_ms_p95": _percentile(latencies, 95),
            "input_tokens": sum(s.input_tokens for s in scores),
            "output_tokens": sum(s.output_tokens for s in scores),
        },
    }


def build_report(scores: list[FixtureScore]) -> dict[str, Any]:
    splits = sorted({s.split for s in scores}, key=lambda x: (x != "synthetic", x))
    return {
        "all": summarize(scores),
        **{split: summarize([s for s in scores if s.split == split]) for split in splits},
    }


def render_markdown(manifest: dict[str, Any], report: dict[str, Any], scores: list[FixtureScore]) -> str:
    cols = [k for k in report if k != "all"] + ["all"]
    rows: list[tuple[str, Any]] = [
        ("Invoices", lambda r: r["invoices"]),
        ("Extraction failed", lambda r: r["extraction_failed"]),
        ("Field accuracy %", lambda r: r["fields"]["accuracy_pct"]),
        ("Critical-field accuracy %", lambda r: r["fields"]["critical_accuracy_pct"]),
        ("Invoices 100% correct", lambda r: r["fields"]["invoices_all_correct"]),
        ("Invoices critical-correct", lambda r: r["fields"]["invoices_critical_correct"]),
        ("Flag recall % (planted)", lambda r: r["flags"]["recall_pct"]),
        ("Flag precision %", lambda r: r["flags"]["precision_pct"]),
        ("False-alarm flags", lambda r: r["flags"]["false_alarms"]),
        ("Correct auto-approvals", lambda r: r["decisions"]["correct_approval"]),
        ("Caught (sent to review)", lambda r: r["decisions"]["caught"]),
        ("Needless reviews", lambda r: r["decisions"]["needless_review"]),
        ("**Unsafe approvals**", lambda r: r["decisions"]["unsafe_approval"]),
        ("Answered by", lambda r: ", ".join(f"{m}: {n}" for m, n in sorted(r["answered_by"].items()))),
        ("LLM calls", lambda r: r["cost_latency"]["llm_calls"]),
        ("Latency p50 / p95 (ms)", lambda r: f"{r['cost_latency']['latency_ms_p50']} / {r['cost_latency']['latency_ms_p95']}"),
        ("Total cost (USD)", lambda r: r["cost_latency"]["total_cost_usd"]),
        ("Mean cost / invoice (USD)", lambda r: r["cost_latency"]["mean_cost_usd"]),
    ]
    out = [
        f"# Eval run `{manifest['run_id']}`",
        "",
        f"Model `{manifest['model']}` (fallback `{manifest['fallback_model']}`), "
        f"prompt `{manifest['prompt_version']}`, schema `{manifest.get('schema_sha', '?')}`, "
        f"thinking `{manifest['thinking_level']}`, "
        f"commit `{manifest.get('git_commit') or '?'}`.",
        "",
        "| Metric | " + " | ".join(cols) + " |",
        "|---|" + "---|" * len(cols),
    ]
    out += [f"| {name} | " + " | ".join(str(fn(report[c])) for c in cols) + " |" for name, fn in rows]

    out += ["", "## Accuracy by field (all)", "", "| Field | Accuracy % | correct | wrong | missing | hallucinated | failed |", "|---|---|---|---|---|---|---|"]
    for name, c in report["all"]["fields"]["by_field"].items():
        out.append(
            f"| `{name}`{' *' if name in CRITICAL else ''} | {c['accuracy_pct']} | "
            + " | ".join(str(c.get(k, 0)) for k in ("correct", "wrong", "missing", "hallucinated", "failed"))
            + " |"
        )
    out.append("\n`*` = critical field (a wrong value must never be auto-approved).")

    out += ["", "## Per invoice", ""]
    for s in scores:
        if not s.wrong_fields and not s.flags.missed and not s.flags.false_alarms and not s.flags.on_bad_extraction:
            continue
        out.append(f"### {s.id} ({s.split}) — {s.decision}{' — EXTRACTION FAILED' if s.failed else ''}")
        for f in s.wrong_fields:
            out.append(f"- {f.outcome}{' (critical)' if f.critical else ''}: `{f.path}` expected `{f.expected}`, got `{f.actual}`")
        for code, path in sorted(s.flags.missed):
            out.append(f"- missed flag: `{code}` on `{path}`")
        for code, path in sorted(s.flags.false_alarms):
            known = " (known check false positive)" if (code, path) in s.known_check_fps else ""
            out.append(f"- false alarm: `{code}` on `{path}`{known}")
        for code, path in sorted(s.flags.on_bad_extraction):
            out.append(f"- flag caught extraction error: `{code}` on `{path}`")
        out.append("")
    return "\n".join(out) + "\n"

