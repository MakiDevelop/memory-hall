# memory-hall benchmarks

Reproducible stress / recall / durability tests that ran during v0.1 validation. Pin the v0.1 baseline so v0.2 improvements can be measured against it.

## What's here

| Script | What it measures |
|---|---|
| [`hit3_recall.py`](hit3_recall.py) | Top-3 recall on 10 hand-curated ground-truth query→entry pairs. **Customize `PAIRS` for your own data before running.** |
| [`concurrency.py`](concurrency.py) | Burst and sustained concurrent write behavior; latency p50/p95/p99, throughput, sync-status breakdown, namespace row verification. |
| [`race.py`](race.py) | 10 simultaneous same-content writes — verifies content-hash dedup under race, no unique-constraint errors. |
| [`results-2026-04-18.md`](results-2026-04-18.md) | v0.1 baseline numbers from the day the engine shipped. |

## Quick run

Requires: a running memory-hall on `http://localhost:9100`, Python 3.12, `httpx` (for `concurrency.py` only).

```bash
python3 hit3_recall.py       # pairs are memhall-developer's hall; edit first
python3 race.py
python3 concurrency.py       # ~5 min, generates ~160 test entries in a scratch namespace
```

## Philosophy

Every benchmark here was designed by a specific agent voice during the v0.1 review (see commit history):

- `hit3_recall.py` — Gemini's demand: "measure recall with Hit@3 on ideal pairs, not gut feeling."
- `concurrency.py` — Codex's redesign of a naive threadpool stress test: async httpx + semaphore, warmup, row verification.
- `race.py` — Gemini Q5: "two agents writing same content must yield one row, no unique-constraint error."

## What this tells you about v0.1

The hot numbers are frozen in `results-2026-04-18.md`. Short version:

- **Write durability**: perfect (zero data loss under 50-way burst, race, Ollama-down).
- **Write throughput**: ~0.5 write/s because the Ollama embed call is sequential. Fast individual writes, slow under burst.
- **Recall (mixed queries)**: Hit@3 = 60%. Pure-English queries score ~100%. Pure-CJK queries score ~0% due to an `unicode61` tokenizer + FTS5 interaction (CJK runs indexed as single tokens, substring queries miss).
- **Known v0.2 targets**: switch CJK tokenization (jieba is the preferred path), parallelize embed (`embed_batch` already in code but unused), fix Dockerfile's `sqlite-vec` wheel (ELFCLASS32 on ARM image → vec0 falls back to brute-force).
