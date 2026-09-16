"""Main entry point for the wealthbraid package."""

from __future__ import annotations

if __name__ == "__main__":
    from wealthbraid.utils.log import setup_logger

    setup_logger(print_version=True)
