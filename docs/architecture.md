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
records/HEAD                      anchor: sequence number and id of the newest record
evidence/sha256/ab/abcdef…        original documents, read-only, named by SHA-256
.wealthbraid/                     lock file only (git-ignored; a lock left by a dead process is recovered)
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
  single `write` + `fsync`. It then rewrites `records/HEAD` atomically, fsyncing
  the directory. The hash chain catches edits, reordering, and interior
  deletion; the anchor catches records removed from the end of the log.
- When folding the log, every record's id, sequence number, and `prev` link are
  checked first. A record that fails is excluded from all state, so reports
  never include tampered content. Reports then show `basis.integrity_issues`,
  and writes are refused until the book verifies.

## Record kinds

| Kind                                 | Meaning                                                      | Sensitive |
| ------------------------------------ | ------------------------------------------------------------ | --------- |
| `evidence`                           | A stored source document                                     | no        |
| `line`                               | One statement row from evidence (not an entry)               | yes       |
| `note`                               | An explanation attached to records                           | no        |
| `account.open` / `account.close`     | Chart of accounts, with dates                                | yes       |
| `entry`                              | A balanced transaction, optionally matching lines            | yes       |
| `correction`                         | Supersedes or voids a current entry, line, or account record | yes       |
| `price`                              | An exchange rate                                             | yes       |
| `reconciliation`                     | A statement balance checked against the ledger               | yes       |
| `operation` / `decision` / `applied` | The workflow itself                                          | —         |

A statement line changes no balance, but it is sensitive: its fingerprint
decides whether a bank row can ever be imported, so a fabricated line could hide
a real one. Only evidence and notes are non-sensitive.

## The operation workflow

```text
propose ──► operation (pending) ──► decision: approve ──► produced records ──► applied
                                 └► decision: reject
```

1. **Propose.** A proposal lists changes with an optional rationale per change.
   It also records the tool, inputs (including the settings used), evidence,
   reasoning summary, confidence, and the head it was made against. It is
   validated by a dry-run against the current book. Invalid proposals are
   refused and nothing is written. A reference field (`evidence`, `lines`,
   `subjects`, `target`) may refer to the id produced by an earlier change in
   the same proposal with `"$N"`. Free text is never rewritten.
2. **Decide.** Only `human:` actors can decide. `system:policy` may approve only
   operations whose changes are all non-sensitive; the projection enforces that.
   Whether policy approves at all is the `policy.auto_apply_non_sensitive`
   setting, which is applied when proposing. Settings are not part of the log,
   so the projection cannot tell whether the setting was on. Approval
   re-validates against the current book. If the book moved on, for example
   because another proposal already matched the same statement line, nothing is
   written.
3. **Apply.** The decision, the produced records, and the `applied` marker are
   appended together. Each produced record names its operation, and the
   projection checks that it matches the proposed change exactly.

The projection enforces provenance as well as accounting. A record written
outside this workflow is excluded from the ledger and reported by
`wealthbraid verify`. That covers an entry without an approved operation and a
decision forged by an agent.

## Corrections

Entries, statement lines, and account open/close records are identified by their
**current version id**. A correction targets that id and becomes the new current
version, or voids it. The original stays in the log, and `trace` shows the whole
chain. A correction that targets a superseded version is refused as stale, so
two reviewers cannot silently overwrite each other.

- Voiding an entry releases the statement lines it matched.
- A statement line can be corrected only while no entry matches it. Voiding it
  releases its fingerprint, so the genuine row can be imported.
- Correcting an `account.open` keeps the account name and may not strand
  existing entries. Voiding an `account.close` reopens the account.

## Statements, matching, reconciliation

Importing stores the file as evidence and proposes one `line` per row, for human
approval. Each line has a fingerprint built from the account, date, amount,
commodity, normalised description, and occurrence number among identical rows in
the file. Bank references are stored but left out of the fingerprint: banks
reuse them across statements, and the same transaction exported with and without
a reference must still match. Re-imports and overlapping statements skip rows
already recorded or already proposed.

Amounts are parsed strictly. Thousands separators must form groups of three, so
a profile with the wrong decimal separator is an error, never a 100× amount.

Categorization proposes one entry per line. An entry that cites statement lines
must post to each statement account exactly the total of the lines it cites
there, and a line can be matched only once.

A reconciliation records the statement balance, the ledger balance, the
difference, and the unmatched lines on that date. `reconciliations` re-checks
every approved reconciliation against today's book and reports one status:

- `balanced`: the ledger now agrees with the statement.
- `changed`: it agreed when approved, but a later change moved that balance.
- `discrepancy`: it never agreed and still doesn't.
- `new_lines`: statement lines inside the period arrived later and are
  unmatched.
- `superseded`: a newer reconciliation covers the same account, date, and
  commodity.

`balanced` and `superseded` need no attention.

## Derived views and reproducibility

Reports, explanations, and scenario runs include a `basis`: the head record id,
the record count, and the parameters. `--at RECORD` projects the log only up to
that record, so any past view can be reproduced exactly.

`explain change` breaks an account's balance change into contributions by
counter account. Within each entry, the explained postings are balanced by the
other postings, so the contributions add up exactly. The result reports this as
`reconciles` rather than assuming it. Scenarios report every figure in cents,
with the reported `growth` as the balancing figure. The identity
`end = start + contributions − withdrawals + growth` therefore holds exactly for
the printed numbers. `growth_exact` and `reconciles` show that the rounding cost
at most two cents.

Net worth rounds converted amounts to cents. It lists each recorded price it
used, with that price's date and age, and warns when a pair's direct and inverse
prices disagree by more than 1%. Holdings with no price path are reported per
side and never netted.

## Security model

wealthbraid runs for one person on a machine they trust.

- The approval rule protects against **mistakes and overreach by agents that
  identify themselves honestly**. An identity is never inferred: every write
  must name its actor, and a terminal being attached proves nothing. A local
  process with shell access can still claim `--actor human:…` or edit files.
  wealthbraid doesn't prevent that; it makes it **detectable**. Every decision
  names its actor, edits to past records break the hash chain, and removing
  records from the end breaks the head anchor. Someone who rewrites the whole
  log and the anchor together is caught only by an external copy, so keep the
  book in git and review the diffs. The log only grows, so those diffs are easy
  to read.
- The web UI has **no authentication**, so it only runs on loopback.
  `wealthbraid serve` binds `127.0.0.1` only. The app refuses requests from
  non-loopback client addresses and requires an exact `Host` of
  `127.0.0.1:<port>` or `localhost:<port>`. The Host check guards against DNS
  rebinding; it is not authentication. Every write needs the per-process token
  and an `Origin` or `Referer` naming this server. Every response, including
  server errors, carries a strict CSP and `Cache-Control: no-store`. Evidence is
  only served as a download. For remote access, use an authenticated tunnel such
  as `ssh -L`.
- wealthbraid makes no network calls. htmx is vendored. `static/VENDOR.json`
  records its source, version, license, and SHA-256, and a test checks the
  digest.

## Known limitations

- The projection is rebuilt from the full log on each command. That's fast for
  personal ledgers, but a cached index would help very large books.
- Lot and cost-basis tracking for investments is not modelled yet. Holdings are
  valued with prices only.
- The fingerprint's occurrence number assumes overlapping statements list
  identical same-day rows consistently. The fingerprint also depends on the
  description, so the same transaction exported with different description text
  is not deduplicated.
- A torn final record (for example after a power loss mid-write) is reported
  with the number of intact records before it, but there is no automatic repair
  command yet.
- The policy setting is not recorded in the log, so the projection cannot tell
  whether a `system:policy` approval of evidence or a note matched the setting
  at the time.
