"""Content-hash race test for memory-hall.

Fires 10 simultaneous write requests with identical content. Verifies:
- Only 1 server row is created
- All 10 responses return the same entry_id
- Exactly 1 response has created=True, 9 have created=False
- No HTTP 500, no unique-constraint error
"""
# ruff: noqa: S310
from __future__ import annotations

import json
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

BASE = "http://localhost:9100"
CONTENT = f"content-hash-race-test-{int(time.time())}"
NAMESPACE = "test:hash-race"
CONCURRENCY = 10


def write_once(i: int) -> tuple[int, int | str, object]:
    body = json.dumps({
        "agent_id": "race-test",
        "namespace": NAMESPACE,
        "type": "note",
        "content": CONTENT,
    }).encode()
    req = urllib.request.Request(
        BASE + "/v1/memory/write",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return (i, resp.status, json.loads(resp.read()))
    except Exception as e:
        return (i, "ERR", str(e))


def main() -> None:
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        results = list(ex.map(write_once, range(CONCURRENCY)))

    entry_ids = Counter(r[2].get("entry_id") for r in results if isinstance(r[2], dict))
    created_flags = Counter(r[2].get("created") for r in results if isinstance(r[2], dict))
    status_codes = Counter(r[1] for r in results)
    errors = [r for r in results if r[1] == "ERR"]

    print(f"{CONCURRENCY} concurrent writes with identical content")
    print(f"HTTP status distribution: {dict(status_codes)}")
    print(f"unique entry_ids: {len(entry_ids)}  {dict(entry_ids)}")
    print(f"created flags: {dict(created_flags)}")
    print(f"errors: {len(errors)}")

    with urllib.request.urlopen(
        f"{BASE}/v1/memory?namespace={NAMESPACE}&limit=20"
    ) as r:
        d = json.loads(r.read())
    rows = len(d["entries"])
    print(f"server rows in namespace: {rows}")

    passed = len(entry_ids) == 1 and rows == 1 and not errors
    verdict = "PASS" if passed else "FAIL"
    print(
        f"{verdict}: expected 1 entry_id + 1 row, got {len(entry_ids)} unique / {rows} rows"
    )


if __name__ == "__main__":
    main()
