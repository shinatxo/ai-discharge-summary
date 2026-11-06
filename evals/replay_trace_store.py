"""Offline replay of the ORCHESTRATOR through the PRODUCTION trace writer (WS1a step build
piece 6, round 4, 6 Nov 2026). No model, no network, no AWS, no timing claims, $0.

    python evals/replay_trace_store.py <batch dir> --out DIR [--piece5 replay.json]

Piece 5's replay (`evals/replay_orchestrate.py`, unchanged: saved model outputs at the
run_step boundary, step-2 input reconstructed — see its docstring) runs every record of the
batch, with its trace writer replaced by a TEE: each item goes first through
`trace_store.TraceStore.put` (typed conversion, size bound, PutItem with
attribute_not_exists(SK)) into ONE in-memory table — the strict `FakeDynamoDB` from
tests/conftest.py (an unimplemented condition is an error, never ignored; Query on PK with
SK in UTF-8 byte order) — and is then kept as plain JSON for the piece-5 checks.

Per record, one Query on PK = GEN#<id> (as infra/trace_query.py issues it) and:
  - items == 12 (11 steps + META; DELIVERY is written by the worker, not the orchestrator —
    separate worker tests), one page, SKs in the expected step-number order;
  - round trip: every stored item, deserialised, equals the item the orchestrator wrote;
  - one ttl for every item = trace_ttl(the run's start) = start + 30 days;
  - no `user_sub` anywhere; no raw line index; no raw notes string; the notes text nowhere
    (unescaped string walk — positive control reported); quotes <= 200;
  - the writer's conservative size bound per item and per run (the item limit is 400,000);
  - the released bundle's hashes equal the committed piece-5 replay's (`--piece5`).
The fake table is not DynamoDB: real request validation, sizes and IAM are item 7.

Writes REPLAY.md and replay.json to --out: counts, codes, sizes, hashes — never note or
output text.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src" / "generate"))
sys.path.insert(0, str(ROOT / "tests"))
import replay_orchestrate as RO  # noqa: E402
import trace_store as TS  # noqa: E402
from boto3.dynamodb.types import TypeDeserializer  # noqa: E402
from conftest import FakeDynamoDB  # noqa: E402  (the tests' strict fake table)
from pipeline import orchestrate as O  # noqa: E402

TABLE = "replay-traces"
# Step-number order taken from the registry (its declaration order), NOT by sorting the SKs —
# so "SKs in step-number order" is checked independently of "byte-sorted".
from pipeline import registry  # noqa: E402
EXPECTED_ORDER = ["META"] + [f"STEP#{s.seq}#{s.name}#A1" for s in registry._ALL_STEPS
                             if s.name != "verify_patient"]
_DES = TypeDeserializer()


def _plain(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v


class Tee:
    """The orchestrator's writer for one record: the production TraceStore first (it raises
    on any failure, so a failed put is never counted), then the plain item for piece 5."""
    store: TS.TraceStore = None

    def __init__(self):
        self.items = []

    def put(self, item):
        Tee.store.put(item)
        self.items.append(json.loads(json.dumps(item)))


def check_record(fake: FakeDynamoDB, gen_id: str, plain_items: list[dict], notes: str) -> dict:
    resp = fake.query(TableName=TABLE, KeyConditionExpression="PK = :pk",
                      ExpressionAttributeValues={":pk": {"S": f"GEN#{gen_id}"}}, ConsistentRead=True)
    typed = resp["Items"]
    stored = [_plain({k: _DES.deserialize(v) for k, v in i.items()}) for i in typed]
    sks = [i["SK"] for i in stored]
    by_sk = {i["SK"]: i for i in plain_items}
    sizes = [TS.item_size_bound(t) for t in typed]
    meta = next((i for i in stored if i["SK"] == "META"), {})
    return {
        "items": len(stored),
        "pages": 2 if "LastEvaluatedKey" in resp else 1,
        "sk_order_ok": sks == EXPECTED_ORDER,
        "byte_sorted": sks == sorted(sks, key=lambda s: s.encode("utf-8")),
        "round_trip_equal": len(stored) == len(plain_items) and all(by_sk.get(i["SK"]) == i for i in stored),
        "ttls": sorted({i["ttl"] for i in stored}),
        "ttl_is_start_plus_30d": None,          # filled by the caller (needs the start epoch)
        "user_sub_present": any("user_sub" in s for i in stored for s in RO.strings(i)),
        "raw_index": sum(O._is_raw_index(n) for i in stored for n in RO._walk(i) if isinstance(n, dict)),
        "raw_notes_strings": sum(isinstance(n.get("notes"), str) for i in stored for n in RO._walk(i)
                                 if isinstance(n, dict)),
        "notes_text_present": RO.notes_text_present(stored, notes),
        "quotes_over_200": sum(isinstance(n.get("quote"), str) and len(n["quote"]) > 200
                               for i in stored for n in RO._walk(i) if isinstance(n, dict)),
        "max_item_bound": max(sizes, default=0),
        "run_bound": sum(sizes),
        "meta_status": meta.get("status"),
    }


def run(batch: Path, piece5: Path | None) -> dict:
    fake = FakeDynamoDB()
    Tee.store = TS.TraceStore(fake, TABLE)
    RO.Writer = Tee                                   # the piece-5 replay, unchanged, with this writer
    p5 = {}
    if piece5 and piece5.exists():
        p5 = {(r["scenario"], r["run"]): r for r in json.loads(piece5.read_text())["results"]}
    manifest = json.loads((batch / "manifest.json").read_text())
    corpus, corpus_sha = RO.rce.load_corpus(RO.rce.DEFAULT_CORPUS_PATH)
    if corpus_sha != manifest["corpus"]["sha256"]:
        raise SystemExit("the corpus has changed since this batch was generated")
    by_id = {sc.id: sc for sc in corpus}

    captured = {}
    real_tee_init = Tee.__init__

    def tee_init(self):
        real_tee_init(self)
        captured["w"] = self
    Tee.__init__ = tee_init

    results = []
    for path in sorted(batch.glob("r*/*.json")):
        rec = json.loads(path.read_text())
        sc = by_id[rec["scenario_id"]]
        if rec["notes_sha256"] != sc.notes_sha256:
            raise SystemExit(f"{rec['generation_id']}: notes_sha256 does not match the corpus")
        r = RO.replay_record(rec, sc.notes)
        gen_id = rec["generation_id"].replace("/", "_")
        t = check_record(fake, gen_id, captured["w"].items, sc.notes)
        start = datetime.fromisoformat(rec["started_at"]).timestamp()
        t["ttl_is_start_plus_30d"] = t["ttls"] == [O.trace_ttl(start)] and O.trace_ttl(start) - int(start) == 30 * 86400
        r["table"] = t
        q = p5.get((r["scenario"], r["run"]))
        if q is not None:
            r["piece5_outcome_equal"] = q["outcome"] == r["outcome"]
            r["piece5_hashes_equal"] = q.get("hashes") == r.get("hashes")
        results.append(r)

    # positive control: the notes-text check fires on a stored multi-line notes string
    probe_notes = next(iter(by_id.values())).notes
    Tee.store.put({"PK": "GEN#positive-control", "SK": "META", "ttl": 1, "x": {"y": [probe_notes]}})
    pc = check_record(fake, "positive-control", [], probe_notes)
    return {"batch": batch.name, "pipeline_version": O.pipeline_version(), "results": results,
            "positive_control": {"notes_multiline": "\n" in probe_notes,
                                 "notes_text_detected": pc["notes_text_present"]},
            "table_items_total": len(fake.items), "summary": summarise(results)}


def summarise(results: list[dict]) -> dict:
    s = RO.summarise(results)
    ok = [r for r in results if r["outcome"] == "bundle"]
    T = [r["table"] for r in results]
    s.update({
        "table_runs": len(T),
        "table_items_12": sum(t["items"] == 12 for t in T),
        "table_one_page": sum(t["pages"] == 1 for t in T),
        "table_sk_order_ok": sum(t["sk_order_ok"] for t in T),
        "table_byte_sorted": sum(t["byte_sorted"] for t in T),
        "table_round_trip_equal": sum(t["round_trip_equal"] for t in T),
        "table_ttl_start_plus_30d": sum(bool(t["ttl_is_start_plus_30d"]) for t in T),
        "table_meta_complete": sum(t["meta_status"] == "complete" for t in T),
        "table_user_sub_present": sum(t["user_sub_present"] for t in T),
        "table_raw_index": sum(t["raw_index"] for t in T),
        "table_raw_notes_strings": sum(t["raw_notes_strings"] for t in T),
        "table_notes_text_present": sum(t["notes_text_present"] for t in T),
        "table_quotes_over_200": sum(t["quotes_over_200"] for t in T),
        "table_max_item_bound": max(t["max_item_bound"] for t in T),
        "table_max_run_bound": max(t["run_bound"] for t in T),
        "piece5_compared": sum("piece5_hashes_equal" in r for r in results),
        "piece5_outcome_equal": sum(r.get("piece5_outcome_equal") is True for r in results),
        "piece5_hashes_equal": sum(r.get("piece5_hashes_equal") is True for r in ok),
    })
    return s


def write(out: Path, rep: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "replay.json").write_text(json.dumps(rep, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    s, pc = rep["summary"], rep["positive_control"]
    lines = [
        f"# Trace-store replay — `{rep['batch']}`", "",
        f"pipeline_version `{rep['pipeline_version']}`. Piece 5's offline orchestrator replay "
        "(saved model outputs; step-2 input reconstructed — see evals/replay_orchestrate.py) with the "
        "PRODUCTION writer (`trace_store.TraceStore`) into the tests' strict fake table; one Query per run. "
        f"No Bedrock request (model_calls {s['model_calls_total']}), no AWS, no timing claims (item 7). "
        "The fake table is not DynamoDB: request validation, real item sizes and IAM are item 7.", "",
        f"- **Records accounted for: {s['accounted_for']}/{s['records']}** — {s['bundles']} released bundles, "
        f"rejections {s['rejections'] or 0}; a document on a failure: {s['document_on_failure']}.",
        f"- **One Query per run: {s['table_items_12']}/{s['table_runs']} runs return 12 items** (11 steps + META; "
        f"DELIVERY is the worker's — worker tests), one page {s['table_one_page']}/{s['table_runs']}, "
        f"SKs in step-number order {s['table_sk_order_ok']}/{s['table_runs']} (UTF-8 byte-sorted "
        f"{s['table_byte_sorted']}/{s['table_runs']}); META complete {s['table_meta_complete']}/{s['table_runs']}.",
        f"- Round trip (stored item deserialised == item written): {s['table_round_trip_equal']}/{s['table_runs']}; "
        f"one ttl per run = start + 30 days: {s['table_ttl_start_plus_30d']}/{s['table_runs']}.",
        f"- In the table: user_sub {s['table_user_sub_present']}; raw line index {s['table_raw_index']}; "
        f"raw notes strings {s['table_raw_notes_strings']}; notes text present {s['table_notes_text_present']} "
        f"(positive control: multi-line notes stored in a probe item → detected {pc['notes_text_detected']}); "
        f"quotes > 200 {s['table_quotes_over_200']}.",
        f"- Size (the writer's conservative bound): largest item {s['table_max_item_bound']:,} bytes "
        f"(limit {O.TRACE_ITEM_MAX_BYTES:,}); largest run {s['table_max_run_bound']:,} bytes (one Query page is "
        f"at most 1 MB). JSON measure as in piece 5: largest item {s['trace_max_item_bytes']:,}, run "
        f"{s['trace_max_total_bytes']:,}.",
        f"- Same as the committed piece-5 replay: outcome {s['piece5_outcome_equal']}/{s['piece5_compared']}, "
        f"bundle hashes {s['piece5_hashes_equal']}/{s['bundles']}. Piece-5 checks unchanged: 8a/8b direct "
        f"{s['direct_8b_equal']}/{s['bundles']}; step 3 vs saved {s['validate_vs_saved']}.",
        f"- Table total: {rep['table_items_total']} items (90 runs × 12 + 1 probe).", "",
    ]
    (out / "REPLAY.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Offline orchestrator replay through the production trace writer.")
    p.add_argument("batch", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--piece5", type=Path, default=None, help="a committed piece-5 replay.json to compare with")
    a = p.parse_args(argv)
    rep = run(a.batch, a.piece5)
    write(a.out, rep)
    print((a.out / "REPLAY.md").read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
