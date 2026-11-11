"""Build the local label-review page for the real eval fixtures.

    python -m scripts.fetch_real_fixtures     # images first
    python -m scripts.prepare_label_review    # full labels -> storage/review/ (gitignored)
    python -m scripts.prepare_label_review --drafts eval/review/drafts/tax_breakdown.json
        # focused review of ONE new field: the draft rows are shown for confirmation,
        # the rest of each (already verified) label is shown read-only

Then serve storage/review/ on 127.0.0.1 (see .claude/launch.json "label-review") and
open it. A human confirms or corrects each label against the photo; the results stay
in that browser and are applied to the fixture JSON afterwards (setting verified_by).

The page stays local on purpose: the photos show a private person's tax number.
"""

import argparse
import json
import shutil
from pathlib import Path

FIXTURES = Path("eval/fixtures/real")
OUT = Path("storage/review")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--drafts", type=Path, help="draft rows for a focused tax_breakdown review")
    args = parser.parse_args()
    drafts = json.loads(args.drafts.read_text(encoding="utf-8")) if args.drafts else None

    (OUT / "img").mkdir(parents=True, exist_ok=True)
    bundle = []
    for path in sorted(FIXTURES.glob("*.json")):
        fx = json.loads(path.read_text(encoding="utf-8"))
        src = FIXTURES / fx["file"]
        if not src.exists():
            raise SystemExit(f"{src} missing: run python -m scripts.fetch_real_fixtures first")
        shutil.copyfile(src, OUT / "img" / src.name)
        item = {**fx, "image": f"img/{src.name}"}
        if drafts is not None:
            draft = drafts[fx["id"]]
            item["expected"] = {**fx["expected"], "tax_breakdown": draft["rows"]}
            item["review_focus"] = "tax_breakdown"
            item["printed_as"] = draft["printed_as"]
        bundle.append(item)
    (OUT / "fixtures.json").write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    shutil.copyfile(Path("eval/review/index.html"), OUT / "index.html")
    mode = " (tax_breakdown review)" if drafts is not None else ""
    print(f"{len(bundle)} fixtures ready in {OUT}/{mode}  (serve it on 127.0.0.1 and open index.html)")


if __name__ == "__main__":
    main()
