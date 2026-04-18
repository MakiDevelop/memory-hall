"""Hit@3 recall benchmark for memory-hall.

Measure top-3 recall on hand-curated ground-truth query→entry pairs across
three search modes (hybrid / semantic / lexical).

**Customize `PAIRS` before running.** The defaults below point to entry_ids
from the memhall primary instance on 2026-04-18; they will not exist in your
hall. Replace them with ideal-match pairs from your own data.

Dependency: stdlib only.
"""
from __future__ import annotations

import json
import urllib.request

BASE_URL = "http://localhost:9100"
SEARCH_NAMESPACES = ["shared", "home"]

# EDIT THESE: (query, expected_entry_id, short note)
PAIRS: list[dict[str, str]] = [
    {"q": "棄用 mem0", "expect": "01HXXXXXXXXXXXXXXXXXXXXX01", "note": "CJK exact topic"},
    {"q": "不再寫入舊記憶系統", "expect": "01HXXXXXXXXXXXXXXXXXXXXX02", "note": "CJK paraphrase"},
    {"q": "七位一體啟用公告", "expect": "01HXXXXXXXXXXXXXXXXXXXXX03", "note": "pure CJK"},
    {"q": "identity confusion", "expect": "01HXXXXXXXXXXXXXXXXXXXXX04", "note": "English in CJK entry"},
    {"q": "deployed to mini primary", "expect": "01HXXXXXXXXXXXXXXXXXXXXX05", "note": "pure English"},
    {"q": "sandbox proxy", "expect": "01HXXXXXXXXXXXXXXXXXXXXX06", "note": "English in mixed entry"},
    {"q": "CJK tokenization 影響", "expect": "01HXXXXXXXXXXXXXXXXXXXXX07", "note": "mixed CJK + English"},
    {"q": "agent-as-user design philosophy", "expect": "01HXXXXXXXXXXXXXXXXXXXXX08", "note": "pure English phrase"},
    {"q": "Tailscale", "expect": "01HXXXXXXXXXXXXXXXXXXXXX09", "note": "single English proper noun"},
    {"q": "跨 session 接力", "expect": "01HXXXXXXXXXXXXXXXXXXXXX10", "note": "CJK paraphrase"},
]


def search(query: str, mode: str = "hybrid", k: int = 3) -> list[str]:
    body = json.dumps({
        "query": query,
        "limit": k,
        "mode": mode,
        "namespace": SEARCH_NAMESPACES,
    }).encode()
    req = urllib.request.Request(
        BASE_URL + "/v1/memory/search",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        d = json.loads(resp.read())
    return [r["entry"]["entry_id"] for r in d.get("results", [])]


def bench(mode: str) -> float:
    hits = 0
    print(f"\n=== mode={mode} ===")
    for p in PAIRS:
        top3 = search(p["q"], mode=mode, k=3)
        hit = p["expect"] in top3
        if hit:
            hits += 1
        pos = top3.index(p["expect"]) + 1 if hit else "miss"
        mark = "✓" if hit else "✗"
        print(f"  [{mark}] pos={str(pos):>4} | q={p['q']!r:40} | {p['note']}")
    score = hits / len(PAIRS)
    print(f"Hit@3 ({mode}): {hits}/{len(PAIRS)} = {score * 100:.0f}%")
    return score


if __name__ == "__main__":
    for mode in ("hybrid", "semantic", "lexical"):
        bench(mode)
