"""The local web UI: where humans review explanations, exceptions, reconciliations, and proposals.

The UI is a thin layer over the same services the CLI uses. It is meant to run
on the loopback interface for the person who owns the book:

* decisions are recorded as ``human:<user>`` from ``wealthbraid.toml``;
* requests must arrive from a loopback client address; the UI has no
  authentication, so it refuses to serve anything else;
* the ``Host`` header must be exactly ``127.0.0.1:<port>`` or ``localhost:<port>``,
  which blocks DNS rebinding (this is not authentication);
* every state-changing request must carry the per-process CSRF token and an
  ``Origin`` or ``Referer`` naming this server, so another web page cannot
  submit approvals;
* every response, including server errors, carries a strict CSP and
  ``Cache-Control: no-store``;
* evidence is always served as a download, never rendered inline;
* no asset is loaded from the network (htmx is vendored; see ``static/VENDOR.json``).
"""

from __future__ import annotations

import datetime as dt
import hmac
import ipaddress
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from wealthbraid.book.book import Book
from wealthbraid.book.state import BookState, OperationState
from wealthbraid.book.verify import verify_book
from wealthbraid.errors import WealthbraidError
from wealthbraid.services.categorize import Assignment, categorize
from wealthbraid.services.explain import explain_change, trace
from wealthbraid.services.reconcile import reconciliation_status
from wealthbraid.services.reports import balances, cashflow, income_statement, net_worth
from wealthbraid.services.review import review_queue

_HERE = Path(__file__).parent
LOOPBACK_NAMES = ("127.0.0.1", "localhost")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; object-src 'none'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
}


def host_allowed(host: str | None, port: int | None) -> bool:
    """Report whether a ``Host`` header names this loopback server exactly.

    Args:
        host: The ``Host`` header value.
        port: The port the server listens on, or ``None`` to accept any numeric port.

    Returns:
        ``True`` for ``127.0.0.1`` or ``localhost`` with the expected port.

    """
    if not host:
        return False
    name, _, host_port = host.partition(":")
    if name not in LOOPBACK_NAMES:
        return False
    if port is None:
        return host_port == "" or host_port.isdigit()
    return host_port == str(port) or (host_port == "" and port == 80)  # noqa: PLR2004


def client_is_loopback(address: str | None) -> bool:
    """Report whether a client address is a loopback IP.

    Args:
        address: The client host from the ASGI scope.

    Returns:
        ``True`` only for loopback IP addresses.

    """
    try:
        return address is not None and ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def _month_bounds(today: dt.date) -> tuple[dt.date, dt.date]:
    start = today.replace(day=1)
    end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    return start, end


def _parse_date(value: str | None, default: dt.date) -> dt.date:
    if not value:
        return default
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, f"invalid date {value!r}") from exc


def account_prefixes(names: Any) -> list[str]:
    """Return every account and ancestor path (including roots), sorted.

    Args:
        names: Account names.

    Returns:
        The sorted, de-duplicated prefixes.

    """
    prefixes = set()
    for name in names:
        parts = name.split(":")
        prefixes.update(":".join(parts[: i + 1]) for i in range(len(parts)))
    return sorted(prefixes)


def change_view(state: BookState, change: Any) -> dict[str, Any]:
    """Prepare a proposed change for display, including a before/after view for corrections.

    Args:
        state: The book state.
        change: A :class:`~wealthbraid.book.schema.ChangeData`.

    Returns:
        A template-friendly dictionary.

    """
    view: dict[str, Any] = {"kind": change.kind, "data": change.data, "rationale": change.rationale}
    if change.kind == "correction":
        target = state.entries.get(change.data.get("target", ""))
        view["before"] = target.data.model_dump(mode="json") if target else None
        view["after"] = change.data.get("replacement")
    if change.kind == "entry":
        view["lines"] = [
            {"id": line_id, **state.lines[line_id].model_dump(mode="json")}
            for line_id in change.data.get("lines", [])
            if isinstance(line_id, str) and line_id in state.lines
        ]
    return view


def operation_view(state: BookState, operation: OperationState) -> dict[str, Any]:
    """Prepare an operation for display.

    Args:
        state: The book state.
        operation: The operation.

    Returns:
        A template-friendly dictionary.

    """
    return {
        "id": operation.id,
        "status": operation.status,
        "tool": operation.data.tool,
        "summary": operation.data.summary,
        "actor": operation.record.actor,
        "is_agent": operation.record.actor.startswith("agent:"),
        "proposed_at": operation.record.recorded_at,
        "reasoning": operation.data.reasoning,
        "confidence": operation.data.confidence,
        "confidence_pct": round(operation.data.confidence * 100),
        "inputs": operation.data.inputs,
        "evidence": [
            {"id": eid, **state.evidence[eid].model_dump(mode="json")}
            for eid in operation.data.evidence
            if eid in state.evidence
        ],
        "changes": [change_view(state, change) for change in operation.data.changes],
        "sensitive": operation.sensitive,
        "stale": bool(state.changed_since(operation)),
        "decision": operation.decision.to_json() if operation.decision else None,
        "results": operation.results,
    }


def create_app(book: Book, *, port: int | None = None) -> FastAPI:  # noqa: PLR0915 - route definitions share the closure
    """Build the web application for a book.

    Args:
        book: The book to serve.
        port: The port the server listens on; the ``Host`` header must name it.

    Returns:
        The FastAPI application.

    """
    app = FastAPI(title="wealthbraid", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    templates = Jinja2Templates(directory=_HERE / "templates")
    csrf_token = secrets.token_urlsafe(32)
    actor = book.config.human_actor

    def render(request: Request, name: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
        state = context.get("state")
        counts = review_queue(state)["counts"] if isinstance(state, BookState) else None
        base = {"book": book.config, "csrf": csrf_token, "actor": actor, "nav_counts": counts, "path": request.url.path}
        return templates.TemplateResponse(request, name, {**base, **context}, status_code=status_code)

    def check_write(request: Request, token: str) -> None:
        if not hmac.compare_digest(token.encode("utf-8"), csrf_token.encode("utf-8")):
            raise HTTPException(403, "invalid form token; reload the page")
        origin = request.headers.get("origin") or request.headers.get("referer")
        if not origin or urlsplit(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "cross-origin or origin-less request refused")

    def secured(response: Response) -> Response:
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Response:
        if not client_is_loopback(request.client.host if request.client else None):
            return secured(PlainTextResponse("wealthbraid only serves loopback clients", status_code=403))
        if not host_allowed(request.headers.get("host"), port):
            return secured(PlainTextResponse("Invalid host header", status_code=400))
        return secured(await call_next(request))

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception) -> Response:
        return secured(PlainTextResponse("Internal Server Error", status_code=500))

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status_code=204)

    @app.exception_handler(WealthbraidError)
    async def domain_error(request: Request, exc: WealthbraidError) -> HTMLResponse:
        status = {"not_found": 404, "policy": 403, "conflict": 409}.get(exc.code, 422)
        return render(request, "error.html", {"error": exc, "state": None}, status_code=status)

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request) -> HTMLResponse:
        state = book.state()
        today = dt.datetime.now().astimezone().date()
        month_start, month_end = _month_bounds(today)
        recent = sorted(state.operations.values(), key=lambda op: op.record.seq, reverse=True)[:8]
        return render(
            request,
            "overview.html",
            {
                "state": state,
                "today": today,
                "worth": net_worth(state, as_of=today, currency=book.config.currency),
                "month": cashflow(state, start=month_start, end=month_end, currency=book.config.currency)["total"],
                "queue": review_queue(state),
                "recent": [operation_view(state, op) for op in recent],
            },
        )

    @app.get("/review", response_class=HTMLResponse)
    def review(request: Request) -> HTMLResponse:
        state = book.state()
        queue = review_queue(state)
        pending = [operation_view(state, state.operations[row["id"]]) for row in queue["pending_operations"]]
        return render(request, "review.html", {"state": state, "queue": queue, "pending": pending})

    @app.get("/operations", response_class=HTMLResponse)
    def operations(request: Request, status: str | None = None) -> HTMLResponse:
        state = book.state()
        ops = sorted(state.operations.values(), key=lambda op: op.record.seq, reverse=True)
        if status:
            ops = [op for op in ops if op.status == status]
        return render(
            request,
            "operations.html",
            {"state": state, "operations": [operation_view(state, op) for op in ops], "status": status},
        )

    @app.get("/operations/{operation_id}", response_class=HTMLResponse)
    def operation_detail(request: Request, operation_id: str) -> HTMLResponse:
        state = book.state()
        operation = state.operations.get(operation_id)
        if operation is None:
            raise HTTPException(404, "operation not found")
        return render(request, "operation.html", {"state": state, "op": operation_view(state, operation)})

    @app.post("/operations/{operation_id}/decide")
    def decide(
        request: Request,
        operation_id: str,
        verdict: str = Form(...),
        note: str = Form(""),
        csrf: str = Form(...),
    ) -> Response:
        check_write(request, csrf)
        error = None
        try:
            book.decide(operation_id, actor=actor, verdict=verdict, note=note.strip() or None)
        except WealthbraidError as exc:
            error = str(exc)
        state = book.state()
        operation = state.operations.get(operation_id)
        if operation is None:
            raise HTTPException(404, "operation not found")
        if request.headers.get("hx-request"):
            return render(
                request,
                "_operation_card.html",
                {"state": state, "op": operation_view(state, operation), "error": error},
            )
        # Redirect to the id stored in the book, never to the raw path parameter.
        return RedirectResponse(f"/operations/{operation.id}", status_code=303)

    @app.get("/lines", response_class=HTMLResponse)
    def lines(request: Request) -> HTMLResponse:
        state = book.state()
        queue = review_queue(state)
        return render(
            request,
            "lines.html",
            {"state": state, "lines": queue["unmatched_lines"], "accounts": sorted(state.accounts)},
        )

    @app.post("/lines/{line_id}/categorize")
    def categorize_line(
        request: Request, line_id: str, account: str = Form(...), note: str = Form(""), csrf: str = Form(...)
    ) -> Response:
        check_write(request, csrf)
        operation, _ = categorize(
            book,
            actor=actor,
            assignments=[
                Assignment(
                    line=line_id,
                    account=account.strip(),
                    confidence=1.0,
                    rationale=note.strip() or "categorized in the web UI",
                )
            ],
            reasoning="Categorized by hand in the review UI.",
        )
        if operation is not None:
            book.decide(operation.id, actor=actor, verdict="approve", note="entered by hand")
        return RedirectResponse("/lines", status_code=303)

    @app.get("/reconciliations", response_class=HTMLResponse)
    def reconciliations(request: Request) -> HTMLResponse:
        state = book.state()
        return render(request, "reconciliations.html", {"state": state, "rows": reconciliation_status(state)})

    @app.get("/entries", response_class=HTMLResponse)
    def entries(request: Request, account: str | None = None) -> HTMLResponse:
        state = book.state()
        rows = [
            version
            for version in reversed(state.sorted_entries())
            if not account
            or any(p.account == account or p.account.startswith(account + ":") for p in version.data.postings)
        ]
        return render(
            request,
            "entries.html",
            {"state": state, "entries": rows[:500], "account": account, "accounts": sorted(state.accounts)},
        )

    @app.get("/trace/{record_id}", response_class=HTMLResponse)
    def trace_page(request: Request, record_id: str) -> HTMLResponse:
        state = book.state()
        return render(request, "trace.html", {"state": state, "t": trace(state, record_id)})

    @app.get("/reports", response_class=HTMLResponse)
    def reports(
        request: Request, start: str | None = None, end: str | None = None, account: str | None = None
    ) -> HTMLResponse:
        state = book.state()
        today = dt.datetime.now().astimezone().date()
        first = _parse_date(start, today.replace(month=1, day=1))
        last = _parse_date(end, today)
        explanation = None
        if account:
            explanation = explain_change(state, account=account, start=first, end=last)
        return render(
            request,
            "reports.html",
            {
                "state": state,
                "start": first,
                "end": last,
                "account": account,
                "accounts": account_prefixes(state.accounts),
                "balances": balances(state, as_of=last),
                "income": income_statement(state, start=first, end=last),
                "cashflow": cashflow(state, start=first, end=last, currency=book.config.currency),
                "explanation": explanation,
            },
        )

    @app.get("/verify", response_class=HTMLResponse)
    def verify(request: Request) -> HTMLResponse:
        return render(request, "verify.html", {"state": book.state(), "report": verify_book(book)})

    @app.get("/evidence/{evidence_id}")
    def evidence(evidence_id: str) -> Response:
        state = book.state()
        data = state.evidence.get(evidence_id)
        if data is None:
            raise HTTPException(404, "evidence not found")
        safe_name = "".join(ch for ch in data.filename if ch.isalnum() or ch in "._-") or "evidence"
        return Response(
            book.store.read_evidence(data.sha256),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
        )

    return app
