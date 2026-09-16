"""Web UI hardening: escaping, request validation, headers, and the vendored asset."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from conftest import AGENT, HUMAN, entry
from fastapi.testclient import TestClient

from wealthbraid.web.app import create_app

BASE = "http://127.0.0.1:8765"
LOCAL = ("127.0.0.1", 50000)
PAYLOAD = '<img src=x onerror="alert(1)">'


@pytest.fixture
def client(funded_book):
    return TestClient(create_app(funded_book, port=8765), base_url=BASE, raise_server_exceptions=False, client=LOCAL)


def _token(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def _hostile_book(book):
    """Put attacker-controlled text into every field an agent or a CSV can set."""
    evidence_id, _ = book.add_evidence(
        b"x", filename=PAYLOAD + ".csv", actor=AGENT, source=PAYLOAD, description=PAYLOAD
    )
    lines = book.propose(
        actor=AGENT,
        tool=PAYLOAD,
        summary=PAYLOAD,
        reasoning=PAYLOAD,
        confidence=0.5,
        inputs={"nested": {"x": PAYLOAD}},
        evidence=[evidence_id],
        changes=[
            {
                "kind": "line",
                "rationale": PAYLOAD,
                "data": {
                    "evidence": evidence_id,
                    "account": "Assets:Bank:Checking",
                    "date": "2026-02-05",
                    "amount": "-7.00",
                    "commodity": "EUR",
                    "description": PAYLOAD,
                    "payee": PAYLOAD,
                    "fingerprint": "f",
                    "row": 2,
                },
            }
        ],
    )
    book.decide(lines.id, actor=HUMAN, verdict="approve", note=PAYLOAD)
    target = book.state().sorted_entries()[0].id
    book.propose(
        actor=AGENT,
        tool="note",
        summary="n",
        reasoning="r",
        confidence=1.0,
        changes=[{"kind": "note", "data": {"subjects": [target], "text": PAYLOAD}}],
    )
    pending = book.propose(
        actor=AGENT,
        tool="t",
        summary=PAYLOAD,
        reasoning=PAYLOAD,
        confidence=0.4,
        changes=[
            {
                "kind": "entry",
                "rationale": PAYLOAD,
                "data": entry(
                    "2026-02-06",
                    ("Expenses:Food", "1"),
                    ("Assets:Bank:Checking", "-1"),
                    payee=PAYLOAD,
                    narration=PAYLOAD,
                ),
            }
        ],
    )
    return lines.id, pending.id, target


def test_agent_controlled_text_is_escaped_everywhere(client, funded_book):
    lines_op, pending_op, entry_id = _hostile_book(funded_book)
    line_id = funded_book.state().operations[lines_op].results[0]
    pages = [
        "/",
        "/review",
        "/operations",
        f"/operations/{lines_op}",
        f"/operations/{pending_op}",
        "/lines",
        "/entries",
        f"/trace/{entry_id}",
        f"/trace/{line_id}",
        "/verify",
        "/reconciliations",
        "/reports",
    ]
    for page in pages:
        html = client.get(page).text
        assert "<img src=x" not in html, page
        assert 'onerror="alert' not in html, page
    assert "&lt;img src=x" in client.get("/review").text


def test_csrf_requires_origin_or_referer(client, funded_book):
    _, pending_op, _ = _hostile_book(funded_book)
    token = _token(client.get("/review").text)
    response = client.post(f"/operations/{pending_op}/decide", data={"verdict": "approve", "csrf": token})
    assert response.status_code == 403
    assert funded_book.state().operations[pending_op].status == "pending"


def test_non_ascii_token_is_rejected_not_a_server_error(client, funded_book):
    _, pending_op, _ = _hostile_book(funded_book)
    response = client.post(
        f"/operations/{pending_op}/decide", data={"verdict": "approve", "csrf": "é"}, headers={"origin": BASE}
    )
    assert response.status_code == 403


def test_categorize_endpoint_checks_token_and_origin(client, funded_book):
    lines_op, _, _ = _hostile_book(funded_book)
    line_id = funded_book.state().operations[lines_op].results[0]
    token = _token(client.get("/lines").text)
    assert (
        client.post(
            f"/lines/{line_id}/categorize", data={"account": "Expenses:Food", "csrf": "bad"}, headers={"origin": BASE}
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/lines/{line_id}/categorize",
            data={"account": "Expenses:Food", "csrf": token},
            headers={"origin": "http://evil.example"},
        ).status_code
        == 403
    )
    assert funded_book.state().unmatched_lines() == [line_id]


@pytest.mark.parametrize(
    "host", ["localhost:8765.evil.com", "127.0.0.1:8765.evil.com", "[::1]:8765", "127.0.0.1:9999", "localhost:abc"]
)
def test_host_header_must_be_exact(funded_book, host):
    app = create_app(funded_book, port=8765)
    response = TestClient(app, base_url=BASE, client=LOCAL).get("/", headers={"host": host})
    assert response.status_code == 400


def test_requests_from_non_loopback_clients_are_refused(funded_book):
    app = create_app(funded_book, port=8765)
    remote = TestClient(app, base_url=BASE, client=("192.168.1.20", 50000))
    assert remote.get("/").status_code == 403


def test_security_headers_on_every_response_including_errors(funded_book):
    app = create_app(funded_book, port=8765)

    @app.get("/boom")
    def boom():
        raise RuntimeError("boom")

    client = TestClient(app, base_url=BASE, raise_server_exceptions=False, client=LOCAL)
    for path, status in (("/", 200), ("/trace/nope", 404), ("/boom", 500)):
        response = client.get(path)
        assert response.status_code == status
        csp = response.headers["content-security-policy"]
        for directive in ("base-uri 'none'", "form-action 'self'", "object-src 'none'", "frame-ancestors 'none'"):
            assert directive in csp, (path, csp)
        assert "data:" not in csp.split("img-src", 1)[1].split(";", 1)[0]
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-frame-options"] == "DENY"


def test_vendored_htmx_matches_its_manifest():
    static = Path(__file__).parents[2] / "src" / "wealthbraid" / "web" / "static"
    manifest = json.loads((static / "VENDOR.json").read_text())
    (item,) = manifest["assets"]
    content = (static / item["file"]).read_bytes()
    assert hashlib.sha256(content).hexdigest() == item["sha256"]
    assert item["version"] == "2.0.4"
