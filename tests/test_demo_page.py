"""The demo page at /: served only in demo mode, self-contained, never renders API text as HTML."""

import re

from app.config import get_settings
from app.main import DEMO_PAGE, app

PAGE = DEMO_PAGE.read_text(encoding="utf-8")


def test_root_redirects_to_docs_outside_demo_mode(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/docs"


def test_root_serves_the_demo_page_in_demo_mode(client):
    base = app.dependency_overrides[get_settings]()
    app.dependency_overrides[get_settings] = lambda: base.model_copy(update={"demo_mode": True})
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "<title>Invoice Extraction Pipeline</title>" in r.text


def test_page_never_renders_strings_as_html():
    # File names and everything the model extracted are untrusted: the page builds every node with textContent.
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", PAGE)


def test_page_loads_nothing_from_other_hosts():
    assert not re.search(r"<(script|link|img|iframe)[^>]+(src|href)=\"https?://", PAGE)


def test_page_only_calls_existing_routes():
    paths = set(app.openapi()["paths"])
    called = set(re.findall(r'(?:api|post)\(`?"?(/[a-z/]+)', PAGE))
    assert {"/demo/scenarios", "/demo/status", "/demo/generate", "/demo/replay/", "/demo/invoices/",
            "/invoices", "/invoices/"} <= called
    assert all(any(p.startswith(c.rstrip("/")) for p in paths) for c in called), called


def test_page_explains_every_check():
    source = "".join(p.read_text(encoding="utf-8") for p in DEMO_PAGE.parents[1].joinpath("checks").glob("*.py"))
    codes = set(re.findall(r'Flag\(\s*"([a-z_]+)"', source)) | set(re.findall(r'_CODE = "([a-z_]+)"', source))
    assert len(codes) >= 13, codes
    for code in codes:
        assert f"{code}:" in PAGE, f"the page has no plain-English text for {code}"
