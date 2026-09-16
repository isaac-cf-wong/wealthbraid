"""Web UI tests: pages render, approvals are recorded as the configured human, and writes are protected."""

from __future__ import annotations

import re

import pytest
from conftest import AGENT, client_for, entry

from wealthbraid.web.app import create_app

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def client(funded_book):
    return client_for(create_app(funded_book, port=8765), base_url=BASE, client=("127.0.0.1", 50000))


def _token(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def _pending(book):
    return book.propose(
        actor=AGENT,
        tool="categorize",
        summary="Lunch at cafe",
        reasoning="Card payment near office.",
        confidence=0.4,
        changes=[
            {
                "kind": "entry",
                "data": entry("2026-02-10", ("Expenses:Food", "12.00"), ("Assets:Bank:Checking", "-12.00")),
            }
        ],
    )


@pytest.mark.parametrize(
    "path", ["/", "/review", "/operations", "/lines", "/reconciliations", "/entries", "/reports", "/verify"]
)
def test_pages_render(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "wealthbraid" in response.text
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_review_shows_reasoning_confidence_and_approves_as_configured_human(client, funded_book):
    operation = _pending(funded_book)
    page = client.get("/review")
    assert "Card payment near office." in page.text
    assert "agent:claude" in page.text
    assert 'value="0.4"' in page.text

    response = client.post(
        f"/operations/{operation.id}/decide",
        data={"verdict": "approve", "note": "fine", "csrf": _token(page.text)},
        headers={"origin": BASE, "hx-request": "true"},
    )
    assert response.status_code == 200
    assert "applied" in response.text
    decided = funded_book.state().operations[operation.id]
    assert decided.status == "applied"
    assert decided.decision.actor == "human:alice"


def test_write_without_token_or_from_other_origin_is_refused(client, funded_book):
    operation = _pending(funded_book)
    token = _token(client.get("/review").text)
    assert (
        client.post(f"/operations/{operation.id}/decide", data={"verdict": "approve", "csrf": "wrong"}).status_code
        == 403
    )
    cross = client.post(
        f"/operations/{operation.id}/decide",
        data={"verdict": "approve", "csrf": token},
        headers={"origin": "http://evil.example"},
    )
    assert cross.status_code == 403
    assert funded_book.state().operations[operation.id].status == "pending"


def test_non_loopback_host_header_is_refused(funded_book):
    rebinding = client_for(create_app(funded_book), base_url="http://attacker.example", client=("127.0.0.1", 50000))
    assert rebinding.get("/").status_code == 400


def test_trace_and_evidence_download(client, funded_book):
    evidence_id, _ = funded_book.add_evidence(b"<script>alert(1)</script>", filename="x.html", actor=AGENT)
    response = client.get(f"/evidence/{evidence_id}")
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment")
    entry_id = funded_book.state().sorted_entries()[0].id
    page = client.get(f"/trace/{entry_id}")
    assert page.status_code == 200
    assert "Proposed by" in page.text
    assert client.get("/trace/ent_missing").status_code == 404


def test_categorize_line_from_ui(client, funded_book):
    evidence_id, _ = funded_book.add_evidence(b"csv", filename="s.csv", actor=AGENT)
    lines = funded_book.propose(
        actor=AGENT,
        tool="import",
        summary="s",
        reasoning="r",
        confidence=1.0,
        changes=[
            {
                "kind": "line",
                "data": {
                    "evidence": evidence_id,
                    "account": "Assets:Bank:Checking",
                    "date": "2026-02-05",
                    "amount": "-7.00",
                    "commodity": "EUR",
                    "description": "BAKERY",
                    "fingerprint": "f",
                    "row": 2,
                },
            }
        ],
    )
    funded_book.decide(lines.id, actor="human:alice", verdict="approve")
    page = client.get("/lines")
    assert "BAKERY" in page.text
    line_id = funded_book.state().unmatched_lines()[0]
    response = client.post(
        f"/lines/{line_id}/categorize",
        data={"account": "Expenses:Food", "csrf": _token(page.text)},
        headers={"origin": BASE},
        follow_redirects=False,
    )
    assert response.status_code == 303
    state = funded_book.state()
    assert state.unmatched_lines() == []
    matched = state.entries[state.line_matches[line_id]]
    assert state.operations[state.by_id[matched.id].operation].decision.actor == "human:alice"
