#!/usr/bin/env python3
# ruff: noqa: I001, E501
from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


DEFAULT_BASE_URL = "http://127.0.0.1:9100"


@dataclass(frozen=True)
class Scenario:
    name: str
    total_requests: int
    max_parallel: int
    duration_s: float | None = None

    @property
    def is_sustained(self) -> bool:
        return self.duration_s is not None


@dataclass
class RequestResult:
    scenario: str
    namespace: str
    request_index: int
    ok: bool
    latency_s: float
    status_code: int | None = None
    entry_id: str | None = None
    embedded: bool | None = None
    sync_status: str | None = None
    created: bool | None = None
    error_kind: str | None = None
    error_detail: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Async concurrency test for memory-hall write endpoint.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--payload-file", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--agent-id", default="codex-concurrency-test")
    parser.add_argument("--namespace-prefix", default="test:concurrency-codex")
    parser.add_argument("--entry-type", default="note")
    parser.add_argument("--warmup-requests", type=int, default=1)
    parser.add_argument("--verify-limit", type=int, default=200)
    parser.add_argument("--json-out", type=Path, default=None)
    return parser.parse_args()


def load_payloads(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return [
            {
                "content": "short payload: queue behavior smoke check",
                "summary": "short",
                "tags": ["short", "warm"],
            },
            {
                "content": (
                    "medium payload: English and 中文 mixed together to exercise tokenization, "
                    "embedding latency, and JSON serialization under concurrent writes."
                ),
                "summary": "medium-mixed",
                "tags": ["medium", "mixed"],
            },
            {
                "content": (
                    "long payload: "
                    + "This paragraph intentionally repeats operational context to create a larger "
                    + "embedding input. " * 12
                    + "最後補一段中文，確認中英混合內容在同一批測試中也能走完整寫入流程。"
                ),
                "summary": "long-mixed",
                "tags": ["long", "mixed"],
            },
        ]

    payloads: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"payload file line {line_no} is not valid JSON: {exc}") from exc
        if isinstance(item, str):
            payloads.append({"content": item})
            continue
        if not isinstance(item, dict):
            raise ValueError(f"payload file line {line_no} must be a JSON object or string")
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"payload file line {line_no} missing non-empty 'content'")
        payloads.append(item)
    if not payloads:
        raise ValueError("payload file contained no usable JSONL rows")
    return payloads


def build_payload(
    *,
    template: dict[str, Any],
    namespace: str,
    agent_id: str,
    entry_type: str,
    unique_token: str,
) -> dict[str, Any]:
    metadata = template.get("metadata")
    if metadata is None:
        metadata_out: dict[str, Any] = {}
    elif isinstance(metadata, dict):
        metadata_out = dict(metadata)
    else:
        raise ValueError("payload metadata must be a JSON object")

    content = template["content"].rstrip() + f"\n\n[mh-test-token] {unique_token}"

    tags = template.get("tags", [])
    references = template.get("references", [])
    if not isinstance(tags, list) or not all(isinstance(item, str) for item in tags):
        raise ValueError("payload tags must be a list of strings")
    if not isinstance(references, list) or not all(isinstance(item, str) for item in references):
        raise ValueError("payload references must be a list of strings")

    payload = {
        "agent_id": template.get("agent_id", agent_id),
        "namespace": namespace,
        "type": template.get("type", entry_type),
        "content": content,
        "summary": template.get("summary"),
        "tags": list(tags),
        "references": list(references),
        "metadata": metadata_out,
    }
    return payload


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    rank = (len(values) - 1) * pct
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return values[low]
    weight = rank - low
    return values[low] * (1 - weight) + values[high] * weight


def classify_exception(exc: Exception) -> tuple[str, str]:
    detail = str(exc) or exc.__class__.__name__
    lowered = detail.lower()
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", detail
    if "connection reset" in lowered or "reset by peer" in lowered or "broken pipe" in lowered:
        return "connection_reset", detail
    if isinstance(exc, httpx.ConnectError):
        if "refused" in lowered:
            return "connection_refused", detail
        return "connect_error", detail
    if isinstance(exc, (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)):
        return "network_error", detail
    return "unexpected_error", detail


async def send_one(
    *,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    scenario: Scenario,
    namespace: str,
    request_index: int,
    payload: dict[str, Any],
) -> RequestResult:
    async with semaphore:
        started = time.perf_counter()
        try:
            response = await client.post("/v1/memory/write", json=payload)
            latency_s = time.perf_counter() - started
        except Exception as exc:
            latency_s = time.perf_counter() - started
            error_kind, error_detail = classify_exception(exc)
            return RequestResult(
                scenario=scenario.name,
                namespace=namespace,
                request_index=request_index,
                ok=False,
                latency_s=latency_s,
                error_kind=error_kind,
                error_detail=error_detail,
            )

        try:
            data = response.json()
        except json.JSONDecodeError:
            data = {}

        if response.is_success and isinstance(data, dict) and data.get("entry_id"):
            return RequestResult(
                scenario=scenario.name,
                namespace=namespace,
                request_index=request_index,
                ok=True,
                latency_s=latency_s,
                status_code=response.status_code,
                entry_id=str(data.get("entry_id")),
                embedded=bool(data.get("embedded")),
                sync_status=data.get("sync_status"),
                created=data.get("created"),
            )

        error_kind = "server_error" if response.status_code >= 500 else "client_error"
        return RequestResult(
            scenario=scenario.name,
            namespace=namespace,
            request_index=request_index,
            ok=False,
            latency_s=latency_s,
            status_code=response.status_code,
            error_kind=error_kind,
            error_detail=response.text[:500],
        )


async def verify_namespace(
    *,
    client: httpx.AsyncClient,
    namespace: str,
    limit: int,
) -> dict[str, Any]:
    cursor: str | None = None
    entries: list[dict[str, Any]] = []
    while True:
        params: list[tuple[str, str | int]] = [("namespace", namespace), ("limit", limit)]
        if cursor:
            params.append(("cursor", cursor))
        response = await client.get("/v1/memory", params=params)
        response.raise_for_status()
        payload = response.json()
        batch = payload.get("entries", [])
        if not isinstance(batch, list):
            raise ValueError("list endpoint returned invalid entries payload")
        entries.extend(batch)
        cursor = payload.get("next_cursor")
        if not cursor:
            break
    sync_counts = Counter(
        entry.get("sync_status", "unknown") for entry in entries if isinstance(entry, dict)
    )
    return {
        "rows": len(entries),
        "sync_counts": dict(sync_counts),
    }


async def warmup(
    *,
    client: httpx.AsyncClient,
    payloads: list[dict[str, Any]],
    namespace_prefix: str,
    agent_id: str,
    entry_type: str,
    count: int,
) -> None:
    if count <= 0:
        return
    namespace = f"{namespace_prefix}:warmup:{int(time.time())}"
    print(f"[warmup] namespace={namespace} requests={count}")
    semaphore = asyncio.Semaphore(1)
    for index in range(count):
        template = payloads[index % len(payloads)]
        payload = build_payload(
            template=template,
            namespace=namespace,
            agent_id=agent_id,
            entry_type=entry_type,
            unique_token=f"warmup-{index}-{int(time.time() * 1000)}",
        )
        result = await send_one(
            client=client,
            semaphore=semaphore,
            scenario=Scenario(name="warmup", total_requests=count, max_parallel=1),
            namespace=namespace,
            request_index=index,
            payload=payload,
        )
        status = result.sync_status or result.error_kind or "unknown"
        print(
            f"[warmup] request={index} ok={result.ok} status={status} "
            f"latency_ms={result.latency_s * 1000:.0f}"
        )


async def run_scenario(
    *,
    client: httpx.AsyncClient,
    payloads: list[dict[str, Any]],
    scenario: Scenario,
    namespace_prefix: str,
    agent_id: str,
    entry_type: str,
    verify_limit: int,
) -> dict[str, Any]:
    namespace = f"{namespace_prefix}:{scenario.name}:{int(time.time())}"
    semaphore = asyncio.Semaphore(scenario.max_parallel)
    tasks: list[asyncio.Task[RequestResult]] = []
    started = time.perf_counter()

    async def schedule_request(index: int, delay_s: float = 0.0) -> RequestResult:
        if delay_s > 0:
            await asyncio.sleep(delay_s)
        template = payloads[index % len(payloads)]
        payload = build_payload(
            template=template,
            namespace=namespace,
            agent_id=agent_id,
            entry_type=entry_type,
            unique_token=f"{scenario.name}-{index}-{int(time.time() * 1000)}",
        )
        return await send_one(
            client=client,
            semaphore=semaphore,
            scenario=scenario,
            namespace=namespace,
            request_index=index,
            payload=payload,
        )

    if scenario.is_sustained:
        assert scenario.duration_s is not None
        interval_s = scenario.duration_s / scenario.total_requests
        for index in range(scenario.total_requests):
            tasks.append(asyncio.create_task(schedule_request(index, delay_s=index * interval_s)))
    else:
        for index in range(scenario.total_requests):
            tasks.append(asyncio.create_task(schedule_request(index)))

    results = await asyncio.gather(*tasks)
    duration_s = time.perf_counter() - started
    verification = await verify_namespace(client=client, namespace=namespace, limit=verify_limit)

    ok_results = [result for result in results if result.ok]
    latencies_ms = sorted(result.latency_s * 1000 for result in ok_results)
    sync_counts = Counter(result.sync_status or "unknown" for result in ok_results)
    created_counts = Counter(
        "created" if result.created else "dedup_or_existing" for result in ok_results
    )
    error_counts = Counter(result.error_kind for result in results if result.error_kind)

    summary = {
        "scenario": scenario.name,
        "namespace": namespace,
        "total_requests": scenario.total_requests,
        "max_parallel": scenario.max_parallel,
        "duration_s": duration_s,
        "entry_id_success_count": len(ok_results),
        "entry_id_success_rate": len(ok_results) / scenario.total_requests if scenario.total_requests else 0,
        "response_sync_counts": dict(sync_counts),
        "response_created_counts": dict(created_counts),
        "server_rows": verification["rows"],
        "server_sync_counts": verification["sync_counts"],
        "error_counts": dict(error_counts),
        "latency_ms": {
            "p50": percentile(latencies_ms, 0.50),
            "p95": percentile(latencies_ms, 0.95),
            "p99": percentile(latencies_ms, 0.99),
            "max": latencies_ms[-1] if latencies_ms else None,
        },
        "throughput_rps": len(ok_results) / duration_s if duration_s > 0 else None,
        "results": [result.__dict__ for result in results],
    }
    return summary


def print_summary(summary: dict[str, Any]) -> None:
    scenario = summary["scenario"]
    print()
    print(f"=== {scenario} ===")
    print(
        f"namespace={summary['namespace']} total={summary['total_requests']} "
        f"parallel={summary['max_parallel']} duration={summary['duration_s']:.2f}s"
    )
    print(
        "entry_id 成功率="
        f"{summary['entry_id_success_count']}/{summary['total_requests']} "
        f"({summary['entry_id_success_rate'] * 100:.1f}%)"
    )
    print(f"response sync_status={json.dumps(summary['response_sync_counts'], ensure_ascii=False)}")
    print(f"server sync_status={json.dumps(summary['server_sync_counts'], ensure_ascii=False)}")
    print(f"server rows={summary['server_rows']}")
    latency = summary["latency_ms"]
    if latency["p50"] is not None:
        print(
            "latency(ms) "
            f"p50={latency['p50']:.0f} "
            f"p95={latency['p95']:.0f} "
            f"p99={latency['p99']:.0f} "
            f"max={latency['max']:.0f}"
        )
    else:
        print("latency(ms) no successful requests")
    throughput = summary["throughput_rps"]
    if throughput is not None:
        print(f"throughput={throughput:.2f} write/s")
    if summary["error_counts"]:
        print(f"errors={json.dumps(summary['error_counts'], ensure_ascii=False)}")


async def async_main(args: argparse.Namespace) -> int:
    payloads = load_payloads(args.payload_file)
    scenarios = [
        Scenario(name="burst-10", total_requests=10, max_parallel=10),
        Scenario(name="burst-50", total_requests=50, max_parallel=50),
        Scenario(name="sustained-100-over-30s", total_requests=100, max_parallel=10, duration_s=30.0),
    ]
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=100)
    timeout = httpx.Timeout(args.timeout)
    summaries: list[dict[str, Any]] = []

    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        timeout=timeout,
        limits=limits,
        headers={"Content-Type": "application/json"},
    ) as client:
        await warmup(
            client=client,
            payloads=payloads,
            namespace_prefix=args.namespace_prefix,
            agent_id=args.agent_id,
            entry_type=args.entry_type,
            count=args.warmup_requests,
        )
        for scenario in scenarios:
            summary = await run_scenario(
                client=client,
                payloads=payloads,
                scenario=scenario,
                namespace_prefix=args.namespace_prefix,
                agent_id=args.agent_id,
                entry_type=args.entry_type,
                verify_limit=args.verify_limit,
            )
            summaries.append(summary)
            print_summary(summary)

    if args.json_out is not None:
        args.json_out.write_text(
            json.dumps({"base_url": args.base_url, "summaries": summaries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print()
        print(f"json summary written to {args.json_out}")

    return 1 if any(summary["error_counts"] for summary in summaries) else 0


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(async_main(args)))


if __name__ == "__main__":
    main()
