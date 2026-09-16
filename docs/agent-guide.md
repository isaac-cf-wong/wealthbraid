---
title: Agent guide
description: How AI agents use the wealthbraid CLI safely.
---

wealthbraid is built so that an AI agent can do the analysis and a human keeps
control of the ledger. This page is the contract an agent can rely on.

## Rules of engagement

1. **Identify yourself.** Pass `--actor agent:<name>` (before the command) or
   set `WEALTHBRAID_ACTOR=agent:<name>`. Every command that writes without an
   actor is refused with exit 6, whether or not a terminal is attached. A
   malformed actor is refused by every command.
2. **Propose, don't decide.** Agents can't approve or reject (exit 6). Balances
   change only after a human approves.
3. **Explain yourself.** Every proposal needs a reasoning summary and a
   confidence in `[0, 1]`. Cite evidence ids. Put per-change rationale in
   `rationale`. Be calibrated: low confidence sorts first in the human's queue
   and is flagged.
4. **Never guess ids.** Read them from `--json` output.

## Output contract

- `--json` (or `WEALTHBRAID_JSON=1`) prints exactly one JSON document to stdout.
- Errors go to stderr as `{"error": {"code": "...", "message": "..."}}`.
- Exit codes: `0` ok · `1` error (`io` for file-system failures) · `2` usage ·
  `3` not found · `4` validation · `5` integrity · `6` policy · `7` conflict.
  Command-line parsing errors (unknown options, missing arguments) use the same
  JSON shape with code `usage`.
- `--actor` and `--book` are global options:
  `wealthbraid --actor agent:claude --book ~/finances <command> ...`.
- `verify` is a report. It prints `{"ok": ..., "problems": [...]}` on stdout and
  exits `5` when the book fails verification.
- Reports also carry `basis.integrity_issues`. When it is non-zero, records that
  fail integrity checks were excluded from the numbers and a warning is printed
  on stderr. Writes are refused until the book verifies.
- Dates are plain `YYYY-MM-DD`.
- Reports carry `basis.as_of_record`. Quote it when you cite numbers, so a human
  can reproduce them with `--at`.

## Typical loop

```bash
export WEALTHBRAID_ACTOR=agent:claude WEALTHBRAID_JSON=1

wealthbraid status                       # what needs attention
wealthbraid import csv aug.csv --profile mybank   # proposes statement lines; a human approves them
wealthbraid lines --unmatched            # rows to account for, once approved
wealthbraid categorize                   # rule-based proposal
wealthbraid categorize --assignments picks.json --reasoning "…"   # your own picks
wealthbraid reconcile Assets:Bank:Checking --date 2026-08-31 --balance 6384.56 --evidence evd_…
wealthbraid explain change Expenses --from 2026-08-01 --to 2026-08-31
wealthbraid note add ent_… --text "Annual insurance premium, paid once a year." --confidence 0.8
```

`picks.json`:

```json
[
    {
        "line": "lin_…",
        "account": "Expenses:Transport",
        "confidence": 0.85,
        "rationale": "NS is the Dutch railway"
    }
]
```

## Generic proposals

Anything the dedicated commands don't cover can be proposed directly. Run
`wealthbraid schema operation` for the full JSON Schema, and
`wealthbraid schema entry` (or `correction`, `price`, …) for each change
payload.

```bash
wealthbraid propose - <<'EOF'
{
  "tool": "agent.split-purchase",
  "summary": "Split a supermarket receipt into groceries and household goods",
  "reasoning": "The receipt (evd_…) itemises 41.20 food and 20.55 cleaning products.",
  "confidence": 0.8,
  "evidence": ["evd_…"],
  "changes": [
    {"kind": "correction", "rationale": "itemised receipt",
     "data": {"target": "ent_…", "reason": "split by receipt",
              "replacement": {"date": "2026-08-19", "lines": ["lin_…"],
                "postings": [
                  {"account": "Assets:Bank:Checking", "amount": "-61.75", "commodity": "EUR"},
                  {"account": "Expenses:Groceries", "amount": "41.20", "commodity": "EUR"},
                  {"account": "Expenses:Household", "amount": "20.55", "commodity": "EUR"}]}}}
  ]
}
EOF
```

A proposal is validated against the book immediately. If it couldn't be applied,
the command fails with exit 4 and writes nothing. Inside one proposal, `"$0"`,
`"$1"`, … in reference fields refer to the ids that earlier changes will
produce. For example, store evidence in change 0 and cite `"$0"` in a note's
`subjects`.

## References inside a proposal

`"$N"` is resolved only in the reference fields `evidence`, `lines`, `subjects`,
and `target` (including inside a correction's `replacement`). Descriptions,
payees, notes, and every other free-text field are stored verbatim, even if they
happen to read `$0`.

An entry that cites statement lines must post, to each statement account, the
exact total of the lines it cites for that account and commodity.

## Corrections

A correction targets the **current version** of an entry, a statement line, an
`account.open`, or an `account.close`. Its `replacement` must be a payload of
the same kind; omit it to void. `entries --json`, `lines --json`, and
`accounts --json` list current versions, and `trace` shows the whole chain. A
stale target fails with a message naming the version that replaced it.

- A statement line can be corrected or voided only while no entry matches it.
- Correcting an `account.open` keeps the account name. The new date must not
  strand existing entries or fall after the close date.
- Voiding an `account.close` reopens the account.
