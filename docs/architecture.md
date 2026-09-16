---
title: Architecture
description: How wealthbraid stores, validates, and traces financial records.
---

wealthbraid has one source of truth: an append-only log of typed records plus
the documents they cite. Everything else is derived from that log and can be
rebuilt at any time. That includes balances, reports, the review queue,
explanations, and the web UI.

## Layers

```text
wealthbraid.cli        agent-facing CLI: --json everywhere, stable exit codes
wealthbraid.web        human-facing review UI (FastAPI + Jinja2 + vendored htmx)
        │
wealthbraid.services   deterministic tools: import, categorize, reconcile,
        │              explain, reports, scenarios, review queue
wealthbraid.book       Book facade (propose / decide), schemas, projection, verify
        │
wealthbraid.store      record envelopes, canonical JSON, append-only log, evidence blobs
wealthbraid.engine     pure double-entry engine: Decimal amounts, accounts,
                       balancing, inventories, prices (no I/O, stdlib only)
```

The engine is pure and has no knowledge of files or JSON. The store only
appends. `BookState` is a pure fold over records. Only `Book.propose` and
`Book.decide` write.

## On disk

```text
wealthbraid.toml                  settings: currency, user, policy, rules, import profiles
records/2026/09.jsonl             append-only log segments, one canonical JSON record per line
evidence/sha256/ab/abcdef…        original documents, read-only, named by SHA-256
.wealthbraid/                     lock file only (git-ignored)
```

A record:

```json
{
    "id": "ent_3f2a…",
    "v": 1,
    "seq": 57,
    "prev": "dec_91c0…",
    "kind": "entry",
    "recorded_at": "2026-09-16T14:06:03Z",
    "actor": "agent:claude",
    "operation": "opr_9c82…",
    "data": {
        "date": "2026-08-04",
        "narration": "ALBERT HEIJN 1043",
        "postings": [
            {
                "account": "Assets:Bank:Checking",
                "amount": "-86.40",
                "commodity": "EUR"
            },
            {
                "account": "Expenses:Groceries",
                "amount": "86.40",
                "commodity": "EUR"
            }
        ],
        "lines": ["lin_9bc6…"],
        "evidence": ["evd_ee43…"]
    }
}
```

- `id` is a kind prefix plus the first 128 bits of the SHA-256 of the canonical
  JSON of every other field. `prev` is the previous record's id. Editing,
  deleting, or reordering any record changes the ids that follow it.
- Amounts are decimal strings. Floats are rejected at the schema boundary.
- Segments are named by write month, but a write never goes into a segment older
  than the newest one. File order is always chain order.
- A write takes an exclusive lock, validates, and appends all new records in a
  single `write` + `fsync`.

## Record kinds

| Kind                                 | Meaning                                            | Sensitive |
| ------------------------------------ | -------------------------------------------------- | --------- |
| `evidence`                           | A stored source document                           | no        |
| `line`                               | One statement row from evidence (not an entry)     | no        |
| `note`                               | An explanation attached to records                 | no        |
| `account.open` / `account.close`     | Chart of accounts, with dates                      | yes       |
| `entry`                              | A balanced transaction, optionally matching lines  | yes       |
| `correction`                         | Supersedes an entry's current version, or voids it | yes       |
| `price`                              | An exchange rate                                   | yes       |
| `reconciliation`                     | A statement balance checked against the ledger     | yes       |
| `operation` / `decision` / `applied` | The workflow itself                                | —         |

## The operation workflow

```text
propose ──► operation (pending) ──► decision: approve ──► produced records ──► applied
                                 └► decision: reject
```

1. **Propose.** A proposal lists changes with an optional rationale per change.
   It also records the tool, inputs (including the settings used), evidence,
   reasoning summary, confidence, and the head it was made against. It is
   validated by a dry-run against the current book. Invalid proposals are
   refused and nothing is written. A change may refer to the id produced by an
   earlier change in the same proposal with `"$N"`.
2. **Decide.** Only `human:` actors can decide. `system:policy` may approve only
   operations whose changes are all non-sensitive, and only when
   `policy.auto_apply_non_sensitive` is on. Approval re-validates against the
   current book. If the book moved on, for example because another proposal
   already matched the same statement line, nothing is written.
3. **Apply.** The decision, the produced records, and the `applied` marker are
   appended together. Each produced record names its operation, and the
   projection checks that it matches the proposed change exactly.

The projection enforces provenance as well as accounting. A record written
outside this workflow is excluded from the ledger and reported by
`wealthbraid verify`. That covers an entry without an approved operation and a
decision forged by an agent.

## Corrections

An entry is identified by its **current version id**. A correction targets that
id and becomes the new current version. The original stays in the log, and
`trace` shows the whole chain. A correction that targets a superseded version is
refused as stale, so two reviewers cannot silently overwrite each other. Voiding
releases any statement lines the entry matched.

## Statements, matching, reconciliation

Importing stores the file as evidence and records one `line` per row. Each line
has a fingerprint: the bank reference when there is one, otherwise the account,
date, amount, commodity, description, and occurrence number. Re-imports and
overlapping statements therefore skip rows already recorded. Categorization
proposes one entry per line. An entry may match a line only if it posts exactly
the line's amount to the line's account, and a line can be matched only once.

A reconciliation records the statement balance, the ledger balance, the
difference, and the unmatched lines on that date. `reconciliations` re-checks
every approved reconciliation against today's book. A later correction that
moves a reconciled balance shows up as `changed`.

## Derived views and reproducibility

Reports, explanations, and scenario runs include a `basis`: the head record id,
the record count, and the parameters. `--at RECORD` projects the log only up to
that record, so any past view can be reproduced exactly.

`explain change` breaks an account's balance change into contributions by
counter account. Within each entry, the explained postings are balanced by the
other postings, so the contributions add up exactly. The result reports this as
`reconciles` rather than assuming it. Scenarios report the per-year identity
`end = start + contributions − withdrawals + growth` along with its rounding
residual.

## Security model

wealthbraid runs for one person on a machine they trust.

- The approval rule protects against **mistakes and overreach by agents that
  identify themselves honestly**. A local process with shell access can still
  claim `--actor human:…` or edit files. wealthbraid doesn't prevent that. It
  makes it **detectable**: every decision names its actor, and any edit to past
  records breaks the hash chain. For stronger assurance, keep the book in git
  and review the diffs. The log only grows, so those diffs are easy to read.
- The web UI binds to loopback and refuses non-loopback `Host` headers (DNS
  rebinding). It requires a per-process token and a same-origin check for every
  write, sends a strict CSP, and serves evidence only as downloads.
- wealthbraid makes no network calls. htmx is vendored.

## Known limitations

- The projection is rebuilt from the full log on each command. That's fast for
  personal ledgers, but a cached index would help very large books.
- Lot and cost-basis tracking for investments is not modelled yet. Holdings are
  valued with prices only.
- The fingerprint's occurrence number assumes overlapping statements list
  identical same-day rows consistently.
