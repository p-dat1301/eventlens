# Search-selection fixture — labeling guide

`search-selection.v1` is a **synthetic** Vietnamese news corpus written for
EventLens tests. Every organization, person and figure is invented (place names
are real only as settings). No publisher text is included, so the files may live
in Git. Real articles must never be copied here: keep licensed text in a private
local directory and commit only IDs, hashes and metadata.

Source of truth: `scripts/rag/search_fixture_corpus.py` (authored text and
labels). Regenerate with:

```sh
uv run python scripts/rag/build_search_fixture.py          # write
uv run python scripts/rag/build_search_fixture.py --check  # CI: must be unchanged
```

## Files

| File | Content |
| --- | --- |
| `articles.jsonl` | 30 `article_document.v1` records (NFC text, paragraphs separated by a blank line). |
| `questions.jsonl` | 40 questions: `question_id`, `query`, `intent`, `answerability`, `required_article_ids`, `required_chunk_ids` (for `paragraph-900-1.v1`), `required_claims`, `temporal_requirement`, `allowed_scope`. |
| `qrels.jsonl` | Graded judgments. `level: "article"` (grade 0/1/2) and `level: "passage"` (exact code-point span `char_start`/`char_end` plus the quote, grade, `answerability`, `temporal_correct`, `citation_support`). |
| `manifest.json` | Version, counts, SHA-256 of each file, corpus digest, label status. The loader refuses files whose digest differs. |

## Grades

- **2** — the passage/article answers the question by itself (citable).
- **1** — related and partially useful (older figure, context, the other side
  of a false premise), but not sufficient alone.
- **0** — not relevant (implicit: anything without a judgment).

Passage spans are chunker-independent: metrics map them onto whatever chunks a
configuration returns (`exact` = one chunk contains the whole span, `overlap` =
a chunk overlaps it). `required_chunk_ids` are a convenience for
`paragraph-900-1.v1` only.

## Answerability

- `answerable` — at least one grade-2 passage exists.
- `no_answer` — nothing in the corpus answers it (q-032, q-033, q-039).
- `false_premise` — the question presumes something the corpus contradicts or
  never states (q-016 the factory move is an unconfirmed rumor; q-034 nobody
  resigned).

## Temporal requirements

- `latest` — the newest report wins (update questions: q-003, q-010, q-011,
  q-024). Older reports are grade 1 with `temporal_correct: false`.
- `two_time_points` — the answer needs both dates (q-009, q-025).
- a date (`2026-02-20`) — the answer is the state on that day.

## Question mix

Names of people/organizations, places, numbers, dates, paraphrases (q-026 "cầu
bắc qua sông Hậu", q-037 "tàu điện ngầm", unaccented q-035), updates, multi-source
discrepancies (q-004 42 vs ~40 ships, q-030 30 vs 34 kiosks), no-answer, false
premise and two-time-point comparisons.

## Status and limits

- Labels are **author labels from a single annotator**; they are not
  independently reviewed. Use `eventlens-rag search-review-sheet` to produce a
  pooled review sheet (`.private/search-review/`) and record a second
  annotator's grades before using numbers for a production decision.
- 30 articles is a diagnostic set: most configurations hit the ceiling. It
  checks mechanics (offsets, filters, fusion, scope, repeatability), not
  production ranking quality. A 2k–5k article private corpus is needed for that.
