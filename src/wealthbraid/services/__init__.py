"""Deterministic services behind the CLI and web UI.

Each service reads :class:`~wealthbraid.book.state.BookState` and, when it
changes anything, goes through :meth:`~wealthbraid.book.book.Book.propose` so
every change carries its provenance and waits for approval when required.
"""
