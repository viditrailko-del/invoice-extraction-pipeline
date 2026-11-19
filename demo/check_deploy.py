"""Check a running demo from the outside: the page, the guards, and every recorded scenario.

    python demo/check_deploy.py https://invoice-extraction-demo.onrender.com
    python demo/check_deploy.py http://localhost:8001            # CI: the prod image on a fresh database

Stdlib only, so it runs anywhere (CI runner, Windows without the app's compiled deps). Spends no live quota:
replays never call Gemini. Each scenario must end where the builder scored it (status + open flags).
"""

from __future__ import annotations

import json
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path

SCENARIOS = Path(__file__).resolve().parent.parent / "app" / "demo" / "scenarios"


def call(url: str, method: str = "GET", headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main(base: str) -> int:
    base = base.rstrip("/")
    sandbox = {"X-Demo-Session": "deploy-check-" + secrets.token_hex(8)}
    failures: list[str] = []

    def expect(what: str, ok: bool) -> None:
        print(("ok   " if ok else "FAIL ") + what)
        if not ok:
            failures.append(what)

    code, body = call(base + "/health")
    expect(f"/health 200 ({body[:60]!r})", code == 200)
    code, body = call(base + "/")
    expect("/ serves the demo page", code == 200 and b"<html" in body[:500].lower())
    expect("POST /demo/reset without a token is refused",
           call(base + "/demo/reset", "POST")[0] in (403, 404))
    expect("/invoices without a sandbox id is 400", call(base + "/invoices")[0] == 400)
    code, body = call(base + "/demo/status")
    expect(f"/demo/status 200 {json.loads(body) if code == 200 else code}", code == 200)

    for path in sorted(SCENARIOS.glob("*.json")):
        sc = json.loads(path.read_text(encoding="utf-8"))
        code, body = call(f"{base}/demo/replay/{sc['name']}", "POST", sandbox)
        if code != 200:
            expect(f"replay {sc['name']}: HTTP {code}", False)
            continue
        inv = json.loads(body)
        got = (inv["status"], sorted((f["code"], f["field_path"]) for f in inv["flags"] if not f["resolved"]))
        want = (sc["expected"]["status"], sorted(tuple(k) for k in sc["expected"]["flags"]))
        expect(f"replay {sc['name']}: {got[0]} {[c for c, _ in got[1]]}", got == want)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8001"))
