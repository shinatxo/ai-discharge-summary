"""Read one generation's step trace from TraceTable — an OPERATOR action (ADR-009 (b);
WS1a step build piece 6, 6 Nov 2026).

Read-only: one Query on PK = GEN#<job_id> (following LastEvaluatedKey if DynamoDB pages the
result past 1 MB — reported, never silent). Run from the operator's own machine with the
operator's own credentials. Never deployed: no Lambda, API route or SPA has a read grant on
TraceTable (HAZ-19), and this file is not baked into the eval image (infra/ is excluded).

On real data a trace may be used only to investigate the generation it records (ADR-009 (b),
DPIA); step-level evaluation uses synthetic data only. An incident export is a separate,
logged, de-identified operator action (item 8 / the incident procedure), not this script.

    python infra/trace_query.py --table discharge-eph-<date>-traces --job-id <job_id>
        prints the run in SK order: SK, step, status, started_at, latency, size — no content
    ... --out run.json
        also writes the full items to a NEW file (mode 0600; refuses to overwrite)

Nothing is logged; content goes only to the file named with --out.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal
from typing import Any

def _plain(v: Any) -> Any:
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else str(v)
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v


def query_run(client: Any, table: str, job_id: str) -> tuple[list[dict], int]:
    """All items of one run, in the order DynamoDB returns them (SK ascending), and the
    number of pages it took. Items are deserialised to plain Python (numbers: int or str)."""
    from boto3.dynamodb.types import TypeDeserializer
    des = TypeDeserializer()
    items, pages, start = [], 0, None
    while True:
        kw = {"TableName": table, "KeyConditionExpression": "PK = :pk",
              "ExpressionAttributeValues": {":pk": {"S": f"GEN#{job_id}"}},
              "ScanIndexForward": True, "ConsistentRead": True}
        if start is not None:
            kw["ExclusiveStartKey"] = start
        resp = client.query(**kw)
        pages += 1
        items += [_plain({k: des.deserialize(v) for k, v in it.items()}) for it in resp.get("Items", [])]
        start = resp.get("LastEvaluatedKey")
        if not start:
            return items, pages


def summary(items: list[dict]) -> list[dict]:
    return [{"SK": i.get("SK"), "step": i.get("step_name") or i.get("outcome_of"),
             "status": i.get("status") or i.get("results"), "started_at": i.get("started_at"),
             "latency_ms": i.get("latency_ms"), "error_code": i.get("error_code"),
             "bytes": len(json.dumps(i, ensure_ascii=False).encode("utf-8"))} for i in items]


def in_step_order(items: list[dict]) -> bool:
    """True when the run's SKs are in ascending UTF-8 byte order (what one Query returns)."""
    sks = [i["SK"].encode("utf-8") for i in items]
    return sks == sorted(sks)


def _write_new(path: str, items: list[dict]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def main(argv: list[str] | None = None, client: Any = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--table", required=True)
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--region", default="eu-west-2")
    ap.add_argument("--out", help="write full items to this NEW file (0600)")
    a = ap.parse_args(argv)
    if client is None:
        import boto3
        client = boto3.client("dynamodb", region_name=a.region)
    items, pages = query_run(client, a.table, a.job_id)
    for row in summary(items):
        print(json.dumps(row))
    print(json.dumps({"items": len(items), "pages": pages, "in_sk_order": in_step_order(items)}))
    if pages > 1:
        print("NOTE: the run needed more than one Query page (DynamoDB returns at most 1 MB per page)",
              file=sys.stderr)
    if a.out:
        _write_new(a.out, items)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
