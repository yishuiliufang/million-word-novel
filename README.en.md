# million-word-novel

[中文](README.md) | **English**

A continuous-production operating system for long-form fiction.

🎯 Let an AI write hundreds of chapters and a million words — and still remember what it wrote.

## Why

Generating one chapter has not been the hard part for a while. Chapter 300 is.

A model's memory is a window that keeps getting truncated. Some tens of thousands of words in, earlier events are either compressed into a vague summary or fall out of context entirely. Characters drift into different personalities, the dead walk back on stage, foreshadowing is planted and never paid off, and the same beat gets rewritten with different nouns. **The model did not get dumber. It genuinely does not remember.**

This project starts from one counterintuitive claim:

> **Context is cache. Disk is memory.**

So prose is not accumulated in the conversation — it lands on disk. Long-term memory is not a summary that gets shorter and shorter, but a queryable Canon database. After each chapter, the system extracts the prose into a structured Canon Delta (who is where, who learned what, what changed hands, which relationships shifted, which threads advanced), validates it, and writes it to the database. The next chapter **queries** what it needs instead of stuffing everything back into the prompt.

The order is not reversible:

```
chapter prose > validated Canon Delta > canon.db > derived summaries > Markdown views
```

Every Markdown file is a derived view, rebuildable from the database at any time. The only sources of truth are the prose and the Canon DB.

## Features

| | |
| --- | --- |
| 🧠 Four-layer memory | Global Canon + structural + current volume + working memory, retrieved rather than truncated |
| 🔒 Audit bound to a hash | The audit binds to the text hash and commit verifies it — change one character and the audit is void |
| 🗄️ Transactional commit | staging + journal + a single SQLite transaction + atomic rename; recover after a crash |
| 🎭 Semantic consistency | Character / knowledge boundary / timeline / location / item / relationship / causality / foreshadowing |
| 🧵 Threads and setups | Registered plantings, overdue detection, dangling-thread blocking |
| ⏪ Replayable | `canon.py state --at-chapter N` rebuilds the world as it was; snapshots are verifiable |
| 🚦 Strict round close | All 12 conditions must pass before the next round, or `ROUND_BLOCKED` |
| 🧪 172 test cases | Including a 10-chapter end-to-end integration run |
| 🪶 Zero dependencies | Python 3.8+ and the standard library, nothing else |

## Requirements

| | |
| --- | --- |
| **Python** | 3.8 or newer. Developed and accepted on 3.11 / 3.12 |
| **Dependencies** | **None.** Standard library only — `sqlite3` included |
| **Not required** | Redis / Kafka / Docker / a cloud database / any third-party Python package |
| **Platform** | Any — pure Python plus SQLite, so Windows / Linux / macOS all work |

## Install

There is nothing to install. Clone or download and it runs:

```bash
git clone https://github.com/yishuiliufang/million-word-novel.git
cd million-word-novel
python tests/run_tests.py        # confirm the environment first
```

Scripts live in `scripts/` and are invoked from the repository root. `novels/caoye/` is a work in progress (31 chapters, ~82k characters) kept as a live example of what each directory actually looks like.

## Quick start

```bash
# 1. Create the project (also creates state/canon.db)
python scripts/novel_state.py init --root "<project>" --title "My Book" \
  --premise "one-line story" --target 1000000 --chars-per-chapter 3000 \
  --chapters-per-round 5 --volumes 5

# 2. Fill state/bible.md (world rules / characters / setups / threads) and
#    state/outline.md (each volume's irreversible turn), then import the bible
#    into Canon (once only; the original is backed up as state/bible.source.md)
python scripts/canon.py --root "<project>" import-md

# 3. Get this round's briefing (the reincarnation entry point; auto-recovers)
python scripts/novel_state.py next --root "<project>"

# 4. Plan first, then write (commit refuses without a validated plan)
python scripts/novel_state.py plan --root "<project>" --chapter 1 --check
python scripts/novel_state.py scene-plan --root "<project>" --chapter 1

# 5. Write the draft (into drafts/ only), then extract it into a Canon Delta
python scripts/novel_state.py extract --root "<project>" --chapter 1 \
  --file "<project>/drafts/ch-0001.md" --out "<project>/state/plans/ch-0001.delta.json"

# 6. Audit: Level 1 text hygiene + Level 2 semantic consistency
python scripts/novel_state.py audit --root "<project>" --chapter 1 \
  --file "<project>/drafts/ch-0001.md" --strict

# 7. Transactional commit (plan / audit / canon / length gates → snapshot → index)
python scripts/novel_state.py commit --root "<project>" --chapter 1

# 8. Close the round (strict; returns ROUND_BLOCKED with exit code 3 if invalid)
python scripts/novel_state.py round-done --root "<project>"

# 9. Export once the target is met
python scripts/export_novel.py --root "<project>" --by-volume --zip
```

The full command surface is in `docs/CLI.md`.

## What it fixes

| The old system's disease | Now |
|---|---|
| Memory = `bible.md[:4000]`, truncated | Four-layer memory + query-driven Canon retrieval (`context_builder.py`) |
| `outline.md` never reached the round briefing | Parsed into volume goals and irreversible turns, fed into L2 structural memory |
| Commit before audit | **The audit binds to a text hash and commit verifies it**; one character changed voids it |
| A bare four-step commit that half-completes on a crash | staging + journal + one SQLite transaction + atomic rename + `recover` |
| `round-done` only did `round+1` | Twelve full-condition checks; failure means `ROUND_BLOCKED` |
| Text hygiene only | Level 2 semantic consistency (character / knowledge / timeline / location / item / relationship / causality / foreshadowing) |
| The polisher invented character actions | `CANON_AFFECTING_EDIT` is refused by default; allowing it forces a re-audit |
| 8-grams were the only repetition test, and it failed the run | Demoted to a hint; `POSSIBLE_PLOT_REPETITION` adds semantic detection |
| Everything depended on hand-typed `--summary` / `--place` | Prose → structured Canon Delta → validation → derived summaries |
| No tests | 172 cases including a 10-chapter end-to-end run (`python tests/run_tests.py`) |

## The loop (one round = one reincarnation)

```
next → chapter_plan → scene_plan → write → audit(L1+L2) → extract(canon delta)
     → canon consistency validation → commit(transaction) → snapshot → index → round-done
```

Roles, each with a single responsibility (see `docs/ARCHITECTURE.md`):

| Role | Entry point |
|---|---|
| Planner | `novel_state.py plan` / `scene-plan` |
| Writer | the model / a subagent / `auto-next --writer` |
| Extractor | `novel_state.py extract`, `auto-next --extractor`, `--llm` |
| Consistency Auditor | `novel_state.py audit` |
| Style Auditor | `audit_quality.py`, `polish_prose.py`, `fix_quotes.py` |
| Canon Manager | `canon.py` |
| Recovery Manager | `novel_state.py recover`, `validate_state.py` |

## Three hard rules

1. **Plan first, audit first, then commit.** Without a plan and a passing audit bound to the current text hash, commit refuses. `--allow-plan` / `--allow-audit` force it through, but record a `GATE_WAIVED` warning that `round-done` reports.
2. **An uncertain judgement never edits Canon — it records a warning.** Delta entries marked `uncertain: true` or with `confidence < 0.5` are dropped and demoted to warnings awaiting human confirmation.
3. **The only stop condition is `total_chars >= target_chars`, but a round must still close legally.** Hit the target and the loop reaches nirvana (STOP); fall short and it reincarnates.

## Generated layout (`init` creates this)

```
<project>/
├── chapters/     ch-0001.txt                  # prose
├── drafts/       ch-0003.md, committed/       # drafts (archived after commit)
├── parts/                                     # long-chapter sections
├── state/
│   ├── progress.json                          # progress (schema 2)
│   ├── canon.db                               # ★ Canon DB (source of truth)
│   ├── snapshots/                             # per-chapter canon snapshots + manual patches
│   ├── plans/  audits/  derived/  tx/         # plans / audit reports / derived JSON / staging
│   ├── bible.md                               # derived view (hand-written source: bible.source.md)
│   ├── outline.md                             # ★ human plan view (a first-class input to `next`)
│   ├── plot-ledger.md  summaries/             # derived views
│   ├── last-context.md                        # reincarnation handover (generated)
│   └── banned-phrases.txt
├── exports/      book.txt, vol-N.txt, STATS.md, <title>.zip
├── references/   craft notes and per-role prompt templates
├── scripts/      every script
└── tests/        the test suite
```

An older project that already has `export/` keeps using it (nothing gets split across two directories).

## Common operations

```bash
# Inspect Canon
python scripts/canon.py --root "<project>" show
python scripts/canon.py --root "<project>" character <name>
python scripts/canon.py --root "<project>" state --at-chapter 40        # replay the world as it was
python scripts/canon.py --root "<project>" verify-snapshots            # prove snapshots rebuild fully

# Correct Canon by hand (a reason is mandatory; everything is traceable)
python scripts/canon.py --root "<project>" update --kind character --ref <name> \
  --field location --value "<place>" --actor "author" --reason "chapter 4 moved them"

# Validate and recover
python scripts/validate_state.py --root "<project>"                    # full state validation
python scripts/novel_state.py recover --root "<project>" --dry-run      # look for interrupted work

# Roll back (nothing is deleted: withdrawn chapters and artifacts go to state/rolled-back/)
python scripts/novel_state.py rollback --root "<project>" --to-chapter 40 --reason "outline changed"

# Adopt an existing manuscript
python scripts/novel_state.py import --root "<project>" --group "<manuscript>/ch*.txt"
python scripts/novel_state.py plan --root "<project>" --volume-plan "42,50,50,50,50"

# Upgrade an old project (deletes nothing)
python scripts/novel_state.py migrate --root "<old project>"
```

## Word counting and stopping

| Mode | Meaning |
|---|---|
| `no_ws` (default) | Non-whitespace characters, matching Chinese platform convention |
| `cjk` | Han characters only |
| `all` | Raw characters including whitespace |

**Only `total_chars >= target_chars` stops the loop.** But hitting a million words is not the same as finishing a novel: `round-done` also checks for overdue setups, dangling threads, a passing audit, and Canon in sync.

## Project layout

```
million-word-novel
├── README.md / README.en.md   # this file
├── SKILL.md                   # the skill body: reincarnation protocol and each role
├── agents/openai.yaml         # agent configuration
├── scripts/                   # every script (24 .py files + schema.sql)
│   ├── novel_state.py         # main entry: init / next / plan / commit / round-done / recover
│   ├── canon.py  canon_db.py  # Canon management and the database layer
│   ├── chapter_plan.py  context_builder.py  extract.py
│   ├── narrative_audit.py  audit_quality.py  polish_prose.py  fix_quotes.py
│   └── schema.sql             # the canonical DDL source for the Canon DB
├── tests/                     # 172 cases plus the end-to-end run
├── docs/                      # architecture / audit / CLI / acceptance / changes
├── references/                # craft notes and per-role prompt templates
└── novels/caoye/              # sample work in progress (31 chapters, ~82k characters)
```

## Tests

```bash
python tests/run_tests.py            # every case; prints PASS/FAIL counts, writes docs/test-report.json
python tests/run_tests.py -v         # verbose
python tests/run_tests.py --clean    # clean up .tmp-tests afterwards
```

## Documentation

| Document | Contents |
|---|---|
| `docs/AUDIT.md` | Phase 1: pre-upgrade repository audit (problems P1–P24, designs worth keeping R1–R17) |
| `docs/ARCHITECTURE.md` | Phase 2: target architecture, four-layer memory, flow, state machines, layout |
| `docs/DDL.sql` | The canonical Canon DB DDL (generated by `canon.py ddl` from `scripts/schema.sql`) |
| `docs/CHANGES.md` | What changed and why, what was kept, and **which compatibility points broke** |
| `docs/CLI.md` | The full CLI reference, with commands that actually run |
| `docs/ACCEPTANCE.md` | Phase 6: end-to-end acceptance report (commands, exit codes, artifacts, test results) |
| `docs/test-report.json` | Machine-readable test report |
| `SKILL.md` | The skill body: reincarnation protocol and each role's duties |
| `references/loop-prompts.md` | Per-role prompt templates |

## License

MIT
