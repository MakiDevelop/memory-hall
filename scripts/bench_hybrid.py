"""Retrieval quality benchmark — RRF vs weighted_linear (α sweep).

Goal: 回答一個問題 — weighted_linear 預設 α=0.3 是否真的比 RRF 在 memhall 的
實際使用場景上更好。ADR 0008 的立場：沒有 benchmark 證據就回退 RRF。

Usage:

  # synthetic mode (default)：跑內建 fixture corpus，directional only
  python scripts/bench_hybrid.py

  # real-corpus mode：指向 running memhall 實例 + 自己的 query 清單
  python scripts/bench_hybrid.py --corpus my-queries.jsonl --base-url http://...

Query file format (jsonl)：
  {"query": "...", "relevant_ids": ["ent_a", "ent_b"], "notes": "..."}

Metrics:
  MRR (mean reciprocal rank), Recall@5, nDCG@10

Exits non-zero if no mode wins on majority of metrics（讓 CI 可選用）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from memory_hall.config import Settings  # noqa: E402
from memory_hall.server.app import create_app  # noqa: E402
from tests.conftest import client_for_app  # noqa: E402


# ---------- Synthetic corpus + embedder ----------------------------------

# 同義詞群組 — 同 group 的詞共享 embedding 維度，模擬 semantic similarity
_SYNONYM_GROUPS: list[tuple[str, ...]] = [
    ("restore", "recover", "resurface", "rebuild", "還原"),
    ("checklist", "list", "playbook", "清單"),
    ("rollout", "deploy", "ship", "release", "部署"),
    ("incident", "outage", "failure", "broken", "事故"),
    ("hybrid", "combined", "fusion", "混合"),
    ("ranking", "scoring", "rank", "排序"),
    ("embedder", "embed", "vector", "嵌入"),
    ("timeout", "stall", "hang", "逾時"),
    ("sqlite", "database", "db", "資料庫"),
    ("memhall", "memory-hall", "memory", "記憶"),
    ("auth", "token", "bearer", "驗證"),
    ("tailscale", "tailnet", "vpn"),
    ("benchmark", "metric", "evaluation", "評估"),
    ("schema", "migration", "table", "結構"),
    ("council", "review", "agent", "協作"),
]

_TOKEN_TO_DIM: dict[str, int] = {}
for idx, group in enumerate(_SYNONYM_GROUPS):
    for token in group:
        _TOKEN_TO_DIM[token.lower()] = idx

_VECTOR_DIM = len(_SYNONYM_GROUPS) + 1  # +1 collision bucket


def _tokenize(text: str) -> list[str]:
    """Lowercase + split on non-alphanumeric, keep CJK runs as single tokens."""
    out: list[str] = []
    buf: list[str] = []
    for ch in text.lower():
        if ch.isalnum():
            buf.append(ch)
        elif "一" <= ch <= "鿿":
            if buf:
                out.append("".join(buf))
                buf = []
            out.append(ch)
        else:
            if buf:
                out.append("".join(buf))
                buf = []
    if buf:
        out.append("".join(buf))
    return out


class SynonymEmbedder:
    """Bag-of-words over synonym groups. Provides realistic semantic similarity
    (synonyms cluster) without perfectly mirroring lexical overlap."""

    def __init__(self) -> None:
        self.dim = _VECTOR_DIM
        self.timeout_s = 2.0

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _tokenize(text):
            dim = _TOKEN_TO_DIM.get(token, self.dim - 1)
            vec[dim] += 1.0
        # L2 normalize
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


# Synthetic corpus — 25 entries covering English + CJK + mixed signals
_CORPUS: list[dict[str, str]] = [
    {"id": "e01", "content": "quokkamode rollout mitigation log"},
    {"id": "e02", "content": "rollout playbook for tomorrow morning"},
    {"id": "e03", "content": "restore recovery list after embed failures"},
    {"id": "e04", "content": "release calendar for next month"},
    {"id": "e05", "content": "daily standup reminders"},
    {"id": "e06", "content": "hybrid ranking marker entry"},
    {"id": "e07", "content": "combined retrieval ranking strategy notes"},
    {"id": "e08", "content": "hybrid combined retrieval ranking strategy details"},
    {"id": "e09", "content": "sqlite WAL corruption incident 2026-04-20"},
    {"id": "e10", "content": "database migration playbook for schema changes"},
    {"id": "e11", "content": "embedder timeout stall during reindex"},
    {"id": "e12", "content": "tailscale ACL setup for admin endpoints"},
    {"id": "e13", "content": "bearer token auth shim ADR 0007"},
    {"id": "e14", "content": "council review session for memhall reliability"},
    {"id": "e15", "content": "benchmark metric evaluation MRR nDCG"},
    {"id": "e16", "content": "撞牆 incident 記錄 — embedder 逾時"},
    {"id": "e17", "content": "記憶 大廳 部署 到 mac mini"},
    {"id": "e18", "content": "資料庫 結構 變更 計畫"},
    {"id": "e19", "content": "驗證 token 旋轉 流程"},
    {"id": "e20", "content": "混合 排序 策略 評估"},
    {"id": "e21", "content": "ollama bge-m3 embedder configuration"},
    {"id": "e22", "content": "phase A reliability patches summary"},
    {"id": "e23", "content": "phase B admin gate proposal"},
    {"id": "e24", "content": "RRF reciprocal rank fusion default"},
    {"id": "e25", "content": "weighted linear alpha tuning experiments"},
]

# Hand-labeled queries — relevance based on intent, not just lexical overlap
_QUERIES: list[dict[str, Any]] = [
    {
        "query": "quokkamode rollout",
        "relevant_ids": ["e01"],
        "kind": "rare_lexical",
    },
    {
        "query": "resurface checklist",
        "relevant_ids": ["e03"],
        "kind": "pure_semantic",
    },
    {
        "query": "hybrid ranking",
        "relevant_ids": ["e08", "e07", "e06"],
        "kind": "mixed",
    },
    {
        "query": "deploy plan",
        "relevant_ids": ["e02", "e04"],
        "kind": "semantic_paraphrase",
    },
    {
        "query": "WAL corruption sqlite",
        "relevant_ids": ["e09"],
        "kind": "rare_lexical",
    },
    {
        "query": "schema migration",
        "relevant_ids": ["e10", "e18"],
        "kind": "mixed_cjk",
    },
    {
        "query": "embedder hang",
        "relevant_ids": ["e11", "e16"],
        "kind": "semantic_paraphrase",
    },
    {
        "query": "admin endpoint auth",
        "relevant_ids": ["e12", "e13", "e23"],
        "kind": "mixed",
    },
    {
        "query": "撞牆",
        "relevant_ids": ["e16"],
        "kind": "cjk_short",
    },
    {
        "query": "資料庫 結構",
        "relevant_ids": ["e18", "e10"],
        "kind": "cjk_mixed",
    },
    {
        "query": "混合排序",
        "relevant_ids": ["e20", "e08", "e07", "e06"],
        "kind": "cjk_semantic",
    },
    {
        "query": "RRF fusion",
        "relevant_ids": ["e24"],
        "kind": "rare_lexical",
    },
    {
        "query": "alpha tuning",
        "relevant_ids": ["e25"],
        "kind": "rare_lexical",
    },
    {
        "query": "incident outage",
        "relevant_ids": ["e09", "e16"],
        "kind": "pure_semantic",
    },
    {
        "query": "benchmark evaluation",
        "relevant_ids": ["e15"],
        "kind": "mixed",
    },
]


# ---------- Metrics -------------------------------------------------------


def reciprocal_rank(ranked_ids: list[str], relevant: set[str]) -> float:
    for i, eid in enumerate(ranked_ids, start=1):
        if eid in relevant:
            return 1.0 / i
    return 0.0


def recall_at_k(ranked_ids: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    hits = sum(1 for eid in ranked_ids[:k] if eid in relevant)
    return hits / len(relevant)


def ndcg_at_k(ranked_ids: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    dcg = 0.0
    for i, eid in enumerate(ranked_ids[:k], start=1):
        if eid in relevant:
            dcg += 1.0 / math.log2(i + 1)
    ideal_hits = min(len(relevant), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0


# ---------- Bench runner --------------------------------------------------


@dataclass
class ModeResult:
    label: str
    mrr: float
    recall_at_5: float
    ndcg_at_10: float
    per_query: list[dict[str, Any]]


async def _seed_corpus(client: httpx.AsyncClient, corpus: list[dict[str, str]]) -> dict[str, str]:
    """Write corpus, return content -> entry_id mapping."""
    mapping: dict[str, str] = {}
    for item in corpus:
        resp = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "bench",
                "namespace": "shared",
                "type": "note",
                "content": item["content"],
            },
        )
        resp.raise_for_status()
        mapping[item["id"]] = resp.json()["entry_id"]
    return mapping


async def _run_queries(
    client: httpx.AsyncClient,
    queries: list[dict[str, Any]],
    id_map: dict[str, str],
    label: str,
) -> ModeResult:
    rrs: list[float] = []
    recalls: list[float] = []
    ndcgs: list[float] = []
    per_query: list[dict[str, Any]] = []

    for q in queries:
        resp = await client.post(
            "/v1/memory/search",
            json={"query": q["query"], "limit": 10, "mode": "hybrid"},
        )
        resp.raise_for_status()
        results = resp.json()["results"]
        ranked_real_ids = [r["entry"]["entry_id"] for r in results]
        relevant = {id_map[eid] for eid in q["relevant_ids"] if eid in id_map}

        rr = reciprocal_rank(ranked_real_ids, relevant)
        r5 = recall_at_k(ranked_real_ids, relevant, 5)
        ndcg = ndcg_at_k(ranked_real_ids, relevant, 10)

        rrs.append(rr)
        recalls.append(r5)
        ndcgs.append(ndcg)
        per_query.append(
            {
                "query": q["query"],
                "kind": q.get("kind", ""),
                "rr": rr,
                "recall_at_5": r5,
                "ndcg_at_10": ndcg,
            }
        )

    return ModeResult(
        label=label,
        mrr=sum(rrs) / len(rrs) if rrs else 0.0,
        recall_at_5=sum(recalls) / len(recalls) if recalls else 0.0,
        ndcg_at_10=sum(ndcgs) / len(ndcgs) if ndcgs else 0.0,
        per_query=per_query,
    )


def _build_app(hybrid_mode: str, alpha: float, tmp_path: Path) -> Any:
    settings = Settings(
        database_path=tmp_path / "bench.sqlite3",
        vector_database_path=tmp_path / "bench-vectors.sqlite3",
        vector_dim=_VECTOR_DIM,
        embed_dim=_VECTOR_DIM,
        hybrid_mode=cast(Literal["weighted_linear", "rrf"], hybrid_mode),
        hybrid_alpha=alpha,
        request_timeout_s=2.0,
        health_embed_timeout_s=1.0,
        api_token=None,
    )
    return create_app(settings=settings, embedder=SynonymEmbedder())


async def _run_synthetic_one(
    label: str,
    hybrid_mode: str,
    alpha: float,
    queries: list[dict[str, Any]],
    tmp_dir: Path,
) -> ModeResult:
    sub = tmp_dir / label.replace("=", "_").replace(" ", "_")
    sub.mkdir(parents=True, exist_ok=True)
    app = _build_app(hybrid_mode, alpha, sub)
    async with client_for_app(app) as client:
        id_map = await _seed_corpus(client, _CORPUS)
        return await _run_queries(client, queries, id_map, label)


async def run_synthetic(alphas: Iterable[float]) -> list[ModeResult]:
    import tempfile

    results: list[ModeResult] = []
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp_dir = Path(raw_tmp)
        results.append(
            await _run_synthetic_one("rrf", "rrf", 0.0, _QUERIES, tmp_dir)
        )
        for alpha in alphas:
            label = f"weighted_linear(α={alpha})"
            results.append(
                await _run_synthetic_one(label, "weighted_linear", alpha, _QUERIES, tmp_dir)
            )
    return results


# ---------- Real-corpus mode ----------------------------------------------


async def run_real_corpus(
    base_url: str,
    queries_file: Path,
    api_token: str | None,
) -> list[ModeResult]:
    """Real-corpus mode: alpha / mode must be set on the running server before
    invocation. This runner only fires queries; switch server config + re-run
    to compare modes."""
    queries = [json.loads(line) for line in queries_file.read_text().splitlines() if line.strip()]
    headers = {"Authorization": f"Bearer {api_token}"} if api_token else {}
    real_id_map = {eid: eid for q in queries for eid in q["relevant_ids"]}

    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=10.0) as client:
        return [
            await _run_queries(
                client,
                queries,
                real_id_map,
                f"server-config@{base_url}",
            )
        ]


# ---------- Reporting -----------------------------------------------------


def print_summary(results: list[ModeResult]) -> None:
    print("\n=== Summary (higher is better) ===")
    print(f"{'mode':<28} {'MRR':>8} {'R@5':>8} {'nDCG@10':>10}")
    print("-" * 56)
    for r in results:
        print(f"{r.label:<28} {r.mrr:>8.4f} {r.recall_at_5:>8.4f} {r.ndcg_at_10:>10.4f}")

    best_mrr = max(results, key=lambda r: r.mrr)
    best_recall = max(results, key=lambda r: r.recall_at_5)
    best_ndcg = max(results, key=lambda r: r.ndcg_at_10)
    print()
    print(f"best MRR:     {best_mrr.label}")
    print(f"best R@5:     {best_recall.label}")
    print(f"best nDCG@10: {best_ndcg.label}")


def print_per_query_diffs(results: list[ModeResult]) -> None:
    print("\n=== Per-query reciprocal rank ===")
    rrf = next((r for r in results if r.label == "rrf"), None)
    if rrf is None:
        return
    queries = [q["query"] for q in rrf.per_query]
    header = f"{'query':<30} " + " ".join(f"{r.label[:14]:>14}" for r in results)
    print(header)
    print("-" * len(header))
    for i, query in enumerate(queries):
        row = f"{query[:30]:<30} "
        row += " ".join(f"{r.per_query[i]['rr']:>14.3f}" for r in results)
        print(row)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--corpus",
        type=Path,
        help="Path to jsonl query file (real-corpus mode). Omit for synthetic.",
    )
    parser.add_argument("--base-url", default="http://localhost:9100")
    parser.add_argument("--api-token", default=None)
    parser.add_argument(
        "--alpha",
        type=float,
        action="append",
        help="α value(s) to sweep. Default: 0.1, 0.3, 0.5, 0.7, 0.9.",
    )
    args = parser.parse_args()

    alphas = args.alpha or [0.1, 0.3, 0.5, 0.7, 0.9]

    if args.corpus:
        if not args.corpus.exists():
            print(f"corpus file not found: {args.corpus}", file=sys.stderr)
            return 2
        results = asyncio.run(
            run_real_corpus(args.base_url, args.corpus, args.api_token)
        )
    else:
        print("Running synthetic benchmark (directional only — confirm with real corpus).")
        results = asyncio.run(run_synthetic(alphas))

    print_summary(results)
    if not args.corpus:
        print_per_query_diffs(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
