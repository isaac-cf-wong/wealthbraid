# wealthbraid

[![Python CI](https://github.com/isaac-cf-wong/wealthbraid/actions/workflows/ci.yml/badge.svg)](https://github.com/isaac-cf-wong/wealthbraid/actions/workflows/ci.yml)
[![Documentation Status](https://github.com/isaac-cf-wong/wealthbraid/actions/workflows/documentation.yml/badge.svg)](https://isaac-cf-wong.github.io/wealthbraid/)
[![License](https://img.shields.io/badge/License-BSD_3--Clause-blue.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

**wealthbraid** is a local-first, AI-native personal wealth manager built on an
append-only, double-entry ledger stored in human-readable files.

AI agents analyze your finances and propose changes through a CLI. You review
explanations, exceptions, reconciliations, and proposals in a local web UI.
Nothing an agent proposes changes a balance until you approve it. Every number
can be traced back to the evidence, the reasoning, and the decision behind it.

- **Append-only and rebuildable.** The book is a directory of JSON Lines
  segments plus the original documents. Records are never overwritten.
  Corrections are new records linked to what they supersede. All state is
  rebuilt from these files, and `wealthbraid verify` checks every content hash,
  the hash chain, and every evidence digest.
- **Double-entry and exact.** Every entry balances per commodity. Amounts are
  decimals, never floats. Multi-currency valuation uses recorded prices.
- **Provenance for every change.** Every change is an _operation_. It records
  the actor, inputs, evidence, reasoning summary, confidence, proposed records,
  approval status, and resulting record ids.
- **Humans approve sensitive changes.** Only a `human:` actor can approve, and
  every command that writes must name its actor explicitly. Only evidence and
  notes, which neither change balances nor decide what enters the book, may be
  applied automatically by policy. Imported statement lines wait for approval.
  Approval re-checks the proposal against the book as it is at that moment.
- **Private by design.** No telemetry and no network calls. wealthbraid never
  calls a model provider itself. The web UI serves only loopback clients and
  loads no external assets.

## Installation

```bash
uv tool install wealthbraid      # or: pip install wealthbraid
# from a checkout:
uv sync && uv run wealthbraid --help
```

## A month in wealthbraid

```bash
wealthbraid init ~/finances --name "Household" --currency EUR --user alice
cd ~/finances && git init        # optional; the log only ever grows

# You set up accounts. Every write names its actor; --actor and --book go before the command.
wealthbraid --actor human:alice open Assets:Bank:Checking Income:Salary Expenses:Groceries --date 2026-01-01 --approve

# An agent imports and categorizes
export WEALTHBRAID_ACTOR=agent:claude
wealthbraid import csv statement.csv --account Assets:Bank:Checking \
  --date-column Date --amount-column Amount --description-column Description
wealthbraid categorize                                   # rules from wealthbraid.toml
wealthbraid categorize --assignments picks.json --reasoning "Merchant names are unambiguous."
wealthbraid reconcile Assets:Bank:Checking --date 2026-08-31 --balance 6384.56

# You review and decide, in the browser or the terminal
wealthbraid serve                                        # http://127.0.0.1:8765
wealthbraid review
wealthbraid --actor human:alice ops approve opr_…   # the imported lines, then the categorization
```

Then ask questions of the book:

```bash
wealthbraid report networth
wealthbraid report cashflow --from 2026-01-01 --to 2026-08-31
wealthbraid explain change Assets:Bank:Checking --from 2026-08-01 --to 2026-08-31
wealthbraid trace ent_…                 # evidence → statement line → proposal → decision → corrections
wealthbraid scenario template > plan.toml && wealthbraid scenario run plan.toml
wealthbraid verify
```

Every report includes the record id it was computed from. `--at <record>`
reproduces a past view exactly.

## Commands

| Area       | Commands                                                                                 |
| ---------- | ---------------------------------------------------------------------------------------- |
| Book       | `init`, `status`, `verify`, `log`, `show`, `trace`, `schema`, `serve`                    |
| Ledger     | `accounts`, `open`, `close`, `entries`, `add`, `correct`, `void`, `price`                |
| Statements | `evidence add/list`, `import csv`, `lines`, `categorize`, `reconcile`, `reconciliations` |
| Workflow   | `propose`, `review`, `ops list/show/approve/reject`, `note add`                          |
| Analysis   | `report balances/income/networth/cashflow`, `explain change`, `scenario run/template`    |

Every command accepts `--json`. See [the agent guide](docs/agent-guide.md) for
the machine contract and [the architecture](docs/architecture.md) for the
design.

## Development

```bash
uv sync
uv run pytest
uv run ruff check src tests && uv run ruff format --check src tests
```

## License

BSD 3-Clause. See [LICENSE](LICENSE).
