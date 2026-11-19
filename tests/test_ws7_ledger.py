"""evals/ws7_ledger.py - the claim and the monthly ledger (ADR-011 D7; slice 2 part 2).

The real botocore S3 client, against a strict fake on 127.0.0.1
(tests/ws7_fakes.py). `offline_aws` applies to every test in this file, so none
can reach a real AWS endpoint. No model is called and nothing is spent.

The clock is injected: no test reads the real date. Every run ID, cost and
figure is invented.

Each refusal has a partner that is not refused, and each "nothing happened"
check has a control showing the check can see the thing.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "evals"), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_pipeline_eval as rpe  # noqa: E402
import ws7_client as wc  # noqa: E402
import ws7_fakes as fakes  # noqa: E402
import ws7_ledger as wl  # noqa: E402
import ws7_reserve as reserve  # noqa: E402
import ws7_store as store  # noqa: E402
import ws7_stub as stubs  # noqa: E402
from ws7_stub import offline_aws  # noqa: E402,F401  (a fixture)

CFG = reserve.load_config()
MARGIN = CFG.input_allowance_margin_percent
CAP = CFG.cap_usd_micros
ATTEMPTS = CFG.ledger_cas_attempts
RES = {t: reserve.reservation(t, 19, MARGIN)["total_usd_micros"] for t in ("pipeline", "v1")}
R = RES["pipeline"]
JOIN_LIMIT_S = 10.0
SENTINEL = "WS7-LEDGER-SENTINEL note text that must never be carried"

DEC = "ws7/ledger/2026-12.json"
NOV = "ws7/ledger/2026-11.json"
W1, W2, W3, W4, W5 = ("2026-12-02T04:00:00Z", "2026-12-09T04:00:00Z", "2026-12-16T04:00:00Z",
                      "2026-12-23T04:00:00Z", "2026-12-30T04:00:00Z")      # invented Wednesdays


class Clock:
    """The injected wall clock."""

    def __init__(self, at: str):
        self.t = store.parse_utc(at)
        self._script: list = []

    def __call__(self) -> datetime:
        if self._script:                        # a scripted reading, then it stays there
            self.t = self._script.pop(0)
        return self.t

    def set(self, at) -> None:
        self.t = store.parse_utc(at) if isinstance(at, str) else at

    def script(self, *times) -> None:
        """The next readings, one per read of the clock: how a test makes time pass
        while a call is in the middle of its S3 requests."""
        self._script = list(times)


@pytest.fixture(autouse=True)
def _offline(offline_aws):  # noqa: F811
    return offline_aws


def make_world(net, cfg=CFG):
    fake = stubs.started(fakes.S3Fake(), net)
    st = store.Store(store.build_client("s3", endpoint_url=fake.url), fakes.BUCKET)
    clock = Clock(W3)
    return SimpleNamespace(fake=fake, store=st, clock=clock, cfg=cfg,
                           ledger=wl.Ledger(st, cfg, clock=clock))


@pytest.fixture
def world(_offline):
    w = make_world(_offline)
    yield w
    w.fake.stop()


def take(world, when: str = W3, target: str = "pipeline", mode: str = "scheduled") -> wl.Claim:
    return wl.claim(world.store, world.cfg, target=target, mode=mode, scheduled_time=when,
                    clock=world.clock)


def start(world, when: str = W3, target: str = "pipeline", mode: str = "scheduled",
          reservation: int | None = None) -> wl.Admission:
    """Thirty seconds after the scheduled time: claim, then book the reservation."""
    world.clock.set(store.parse_utc(when) + timedelta(seconds=30))
    held = take(world, when, target, mode)
    return world.ledger.admit(held, RES[target] if reservation is None else reservation)


def report(admitted: wl.Admission, total_nanos: int = 1_903_211_000, *, unmetered: bool = False,
           **extra) -> dict:
    """What Guard.report() gives settle() for this run, with invented totals: the
    run's target and reservation, and the two batches part 5 opens."""
    batch = {"planned": 1, "started": 1, "total_usd_nanos": 0}
    out = {"target": admitted.target, "unmetered": unmetered, "total_usd_nanos": total_nanos,
           "total_usd_micros": reserve.to_micros(total_nanos),
           "reservation_usd_nanos": admitted.reservation_usd_micros * 1000,
           "batches": {f"{admitted.run_id}.preflight": dict(batch),
                       f"{admitted.run_id}.main": dict(batch)}}
    out.update(extra)
    return out


def stopped(call, expect: str | None = None) -> store.Stop:
    with pytest.raises(store.Stop) as exc:
        call()
    assert exc.value.code in wl.CODES, exc.value.code
    if expect is not None:
        assert exc.value.code == expect, exc.value.line()
    return exc.value


def entry_of(world, run: str) -> dict:
    for key in world.fake.keys():
        if store.ledger_month(key):
            for e in world.fake.json(key)["entries"]:
                if e["run_id"] == run:
                    return e
    raise AssertionError(f"no entry for {run}")


def ledger_bodies(world) -> dict:
    return {k: world.fake.body(k) for k in world.fake.keys() if store.ledger_month(k)}


def ledger_puts(world) -> list:
    return [p for p in world.fake.puts if store.ledger_month(p[0])]


# --- seeding a ledger as another writer would have left it

def an_entry(run: str, state: str = wl.SETTLED, *, mode: str = "scheduled", target: str = "pipeline",
             kind=None, basis=None, reserved: int = R, counted: int | None = None,
             estimate=None, opened: str = "2026-12-02T04:00:30Z",
             expires: str = "2026-12-02T06:00:30Z") -> dict:
    closed = None if state == wl.OPEN else "2026-12-02T04:40:00Z"
    if state == wl.SETTLED:
        basis = basis or wl.METERED
    if state in (wl.UNRESOLVED, wl.RESOLVED):
        kind = kind or wl.ABANDONED
        estimate = reserved if estimate is None else estimate
    if counted is None:
        counted = reserved if state in (wl.OPEN, wl.UNRESOLVED) else 1_000_000
    return {"run_id": run, "target": target, "mode": mode, "state": state, "kind": kind,
            "basis": basis, "reserved_usd_micros": reserved, "counted_usd_micros": counted,
            "estimate_usd_micros": estimate, "opened_at": opened, "expires_at": expires,
            "closed_at": closed, "resolved_at": "2026-12-03T09:00:00Z" if state == wl.RESOLVED else None}


_seq = itertools.count(1)


def a_ledger(month: str, entries: list, *, waived: bool = False, rev: int = 1) -> dict:
    return {"schema": wl.LEDGER_SCHEMA, "month": month, "rev": rev,
            "write_id": f"{next(_seq):032x}", "cap_usd_micros": CAP, "assurance_waived": waived,
            "entries": entries}


def seed(world, month: str, entries: list, **kw) -> None:
    world.fake.seed(store.ledger_key(month), wl._dump(a_ledger(month, entries, **kw)))


def rewrite(world, key: str, change) -> None:
    """Another writer's compare-and-swap, landed: the ledger changed, with a new
    revision, write ID and ETag."""
    doc = world.fake.json(key)
    change(doc)
    doc["rev"] += 1
    doc["write_id"] = f"{next(_seq):032x}"
    world.fake.seed(key, wl._dump(doc))


def add_settled(run: str):
    return lambda doc: doc["entries"].append(an_entry(run, mode="manual"))


# =============================================================== the claim ==

def test_a_claim_is_one_object_written_with_if_none_match(world):
    world.clock.set("2026-12-16T04:00:37Z")
    held = take(world)
    key = "ws7/claims/pipeline/2026-12-16"
    assert (held.key, held.run_id, held.month) == (key, "2026-12-16T0400-pipeline", "2026-12")
    assert world.fake.puts == [(key, "if-none-match", 200)]               # the header, at the server
    assert world.fake.json(key) == {
        "schema": "ws7-claim/1", "target": "pipeline", "mode": "scheduled",
        "scheduled_time": "2026-12-16T04:00:00Z", "run_id": "2026-12-16T0400-pipeline",
        "claim_id": held.claim_id, "claimed_at": "2026-12-16T04:00:37Z"}
    assert world.fake.requests == [("PUT", key)]                          # no ledger was read


def test_a_second_delivery_is_a_duplicate_that_reads_no_ledger_and_writes_nothing(world):
    world.clock.set("2026-12-16T04:00:37Z")
    first = take(world)
    seed(world, "2026-12", [an_entry("2026-12-02T0400-pipeline")])        # a ledger that could be read
    before = {k: world.fake.body(k) for k in world.fake.keys()}
    seen = len(world.fake.requests)

    stop = stopped(lambda: take(world), wl.DUPLICATE)
    assert isinstance(stop, wl.Duplicate) and stop.metric == "Duplicate"
    assert stop.ids == (first.run_id,) and stop.counts == {"duplicates": 1}
    assert world.fake.requests[seen:] == [("PUT", first.key)]             # one refused write; no read
    assert world.fake.puts[-1] == (first.key, "if-none-match", 412)
    assert {k: world.fake.body(k) for k in world.fake.keys()} == before   # nothing reserved

    # The same occurrence delivered with a later time that day is still a duplicate.
    world.clock.set("2026-12-16T04:20:00Z")
    stopped(lambda: take(world, "2026-12-16T04:10:00Z"), wl.DUPLICATE)
    # The partners: another target, and another date, are their own occurrences.
    assert take(world, target="v1").run_id == "2026-12-16T0400-v1"
    world.clock.set("2026-12-23T04:00:30Z")
    assert take(world, W4).run_id == "2026-12-23T0400-pipeline"


def test_a_manual_run_on_the_scheduled_date_does_not_make_the_scheduled_run_a_duplicate(world):
    """Change 1. A manual run started before 04:00 on a Wednesday takes a key of its
    own, so the scheduled delivery still claims, and the Thursday check still finds
    the scheduled run's claim under the scheduled key."""
    world.clock.set("2026-12-16T01:30:20Z")
    manual = take(world, "2026-12-16T01:30:00Z", mode="manual")
    assert manual.key == "ws7/claims/pipeline/manual/2026-12-16T0130-pipeline-manual"
    assert manual.run_id == "2026-12-16T0130-pipeline-manual"

    world.clock.set("2026-12-16T04:00:37Z")
    scheduled = take(world)                                               # not a duplicate
    assert scheduled.key == "ws7/claims/pipeline/2026-12-16"
    assert world.fake.json(scheduled.key)["mode"] == "scheduled"
    assert {manual.run_id, scheduled.run_id} == {"2026-12-16T0130-pipeline-manual",
                                                 "2026-12-16T0400-pipeline"}
    # The partner: a second scheduled delivery for that date IS a duplicate, and so is
    # the same manual run started twice.
    stopped(lambda: take(world), wl.DUPLICATE)
    world.clock.set("2026-12-16T01:30:40Z")
    stopped(lambda: take(world, "2026-12-16T01:30:00Z", mode="manual"), wl.DUPLICATE)


def test_two_deliveries_at_once_give_exactly_one_claim(world, _offline):
    world.clock.set("2026-12-16T04:00:37Z")
    barrier, results = threading.Barrier(2, timeout=JOIN_LIMIT_S), []

    def deliver():
        st = store.Store(store.build_client("s3", endpoint_url=world.fake.url), fakes.BUCKET)
        barrier.wait()
        try:
            results.append(("held", wl.claim(st, CFG, target="pipeline", mode="scheduled",
                                             scheduled_time=W3, clock=world.clock).claim_id))
        except wl.Duplicate:
            results.append(("duplicate", None))

    threads = [threading.Thread(target=deliver, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(JOIN_LIMIT_S)
        assert not t.is_alive(), "a delivery did not finish within its limit"
    assert sorted(kind for kind, _ in results) == ["duplicate", "held"]
    winner = next(claim_id for kind, claim_id in results if kind == "held")
    assert world.fake.json("ws7/claims/pipeline/2026-12-16")["claim_id"] == winner
    assert sorted(p[2] for p in world.fake.puts) == [200, 412]


@pytest.mark.parametrize("script", [fakes.LAND_THEN_500, fakes.LAND_THEN_DROP])
def test_a_claim_that_landed_but_whose_reply_failed_is_recognised_by_its_claim_id(world, script):
    """Change 4. The write is not assumed lost, nor assumed won: the object is read
    back (listing first) and it carries this job's claim_id."""
    world.clock.set("2026-12-16T04:00:37Z")
    key = "ws7/claims/pipeline/2026-12-16"
    world.fake.script_put(key, script)
    started = time.monotonic()
    held = take(world)
    assert time.monotonic() - started < JOIN_LIMIT_S
    assert world.fake.json(key)["claim_id"] == held.claim_id
    assert world.fake.requests == [("PUT", key), ("GET", "?list"), ("GET", key)]


def test_a_claim_whose_reply_failed_and_which_another_job_holds_is_a_duplicate(world):
    world.clock.set("2026-12-16T04:00:37Z")
    key = "ws7/claims/pipeline/2026-12-16"
    theirs = {"schema": "ws7-claim/1", "target": "pipeline", "mode": "scheduled",
              "scheduled_time": W3, "run_id": "2026-12-16T0400-pipeline",
              "claim_id": "f" * 32, "claimed_at": "2026-12-16T04:00:36Z"}
    world.fake.before_put(key, lambda: world.fake.seed(key, wl._dump(theirs)))
    world.fake.script_put(key, fakes.FAIL_500)
    stopped(lambda: take(world), wl.DUPLICATE)
    assert world.fake.json(key)["claim_id"] == "f" * 32                   # theirs, untouched


def test_a_claim_is_tried_again_after_a_409_and_given_up_after_the_configured_tries(world):
    world.clock.set("2026-12-16T04:00:37Z")
    key = "ws7/claims/pipeline/2026-12-16"
    world.fake.script_put(key, *[fakes.CONFLICT_409] * (ATTEMPTS - 1))
    assert take(world).key == key                                         # the last try wins
    assert world.fake.count("PUT", key) == ATTEMPTS

    v1_key = "ws7/claims/v1/2026-12-16"
    world.fake.script_put(v1_key, *[fakes.CONFLICT_409] * ATTEMPTS)
    stop = stopped(lambda: take(world, target="v1"), wl.CLAIM_UNAVAILABLE)
    assert stop.ids == ("2026-12-16T0400-v1", store.CREATE_UNSURE)
    assert world.fake.count("PUT", v1_key) == ATTEMPTS and v1_key not in world.fake.keys()


def test_a_claim_that_s3_refuses_outright_is_not_held(world):
    world.clock.set("2026-12-16T04:00:37Z")
    world.fake.script_put("ws7/claims/pipeline/2026-12-16", fakes.DENY_403)
    stop = stopped(lambda: take(world), wl.CLAIM_UNAVAILABLE)
    assert stop.ids[1] == store.PUT_REFUSED
    assert take(world)                                                    # the partner: no script


@pytest.mark.parametrize("mode", ["scheduled", "manual"])
def test_the_scheduled_time_must_be_recent_and_not_in_the_future(world, mode):
    """`max_start_delay_s` is the oldest scheduled time a run may still claim, for a
    scheduled and a manual run alike. A refused start writes nothing."""
    late = store.parse_utc(W3) + timedelta(seconds=CFG.max_start_delay_s)
    world.clock.set(late + timedelta(seconds=1))
    stopped(lambda: take(world, mode=mode), wl.START_TOO_LATE)
    world.clock.set(store.parse_utc(W3) - timedelta(seconds=1))
    stopped(lambda: take(world, mode=mode), wl.SCHEDULED_TIME_FUTURE)
    assert world.fake.requests == []                                      # no write, no read

    world.clock.set(late)                                                 # exactly at the limit
    assert take(world, mode=mode)
    world.clock.set(W3)                                                   # exactly on time
    assert take(world, target="v1", mode=mode)


@pytest.mark.parametrize("bad", ["2026-12-16T04:00:00", "2026-12-16 04:00:00Z", "2026-12-16",
                                 "2026-13-40T04:00:00Z", "yesterday", "", None, 1797393600,
                                 f"2026-12-16T04:00:00Z {SENTINEL}"])
def test_a_scheduled_time_without_a_zone_or_out_of_shape_is_refused(world, bad):
    stop = stopped(lambda: take(world, bad), wl.SCHEDULED_TIME_INVALID)
    assert SENTINEL not in stop.line() and world.fake.requests == []
    # The partners: Z, and an explicit offset for the same instant.
    assert take(world, "2026-12-16T04:00:00Z").scheduled_time == W3
    assert take(world, "2026-12-16T05:00:00+01:00", target="v1").scheduled_time == W3


def test_an_unknown_target_or_mode_is_refused(world):
    stopped(lambda: take(world, target="canary"), wl.TARGET_UNKNOWN)
    stopped(lambda: take(world, mode="weekly"), wl.MODE_UNKNOWN)
    assert world.fake.requests == []


# ======================================================= compare-and-swap ==

def test_a_months_first_ledger_is_created_with_if_none_match_and_then_written_with_if_match(world):
    first = start(world, W1)
    assert ledger_puts(world) == [(DEC, "if-none-match", 200)]
    created = world.fake.etag(DEC)
    world.ledger.settle(first, report(first))
    start(world, W2)
    assert ledger_puts(world) == [(DEC, "if-none-match", 200), (DEC, "if-match", 200),
                                  (DEC, "if-match", 200)]
    doc = world.fake.json(DEC)
    assert (doc["rev"], doc["month"], doc["cap_usd_micros"]) == (3, "2026-12", CAP)
    assert world.fake.etag(DEC) != created


def test_a_write_that_lost_the_race_is_decided_again_from_a_fresh_read(world):
    """Another writer lands between this job's read and its write. S3 answers 412;
    the job reads again and writes on top, so neither change is lost."""
    first = start(world, W1)
    world.ledger.settle(first, report(first))
    world.fake.before_put(DEC, lambda: rewrite(world, DEC, add_settled("2026-12-05T1000-v1-manual")))
    second = start(world, W2)
    assert ledger_puts(world)[-2:] == [(DEC, "if-match", 412), (DEC, "if-match", 200)]
    runs = [e["run_id"] for e in world.fake.json(DEC)["entries"]]
    assert runs == [first.run_id, "2026-12-05T1000-v1-manual", second.run_id]


def test_a_first_ledger_another_job_created_first_is_read_and_written_on_top(world):
    world.fake.before_put(DEC, lambda: seed(world, "2026-12", [an_entry("2026-12-01T1000-v1-manual",
                                                                        mode="manual")]))
    mine = start(world, W1)
    assert ledger_puts(world) == [(DEC, "if-none-match", 412), (DEC, "if-match", 200)]
    assert [e["run_id"] for e in world.fake.json(DEC)["entries"]] \
        == ["2026-12-01T1000-v1-manual", mine.run_id]


def _lose(world, times: int, how: str) -> None:
    """Make the next `times` writes to December's ledger lose."""
    if how == "409":
        world.fake.script_put(DEC, *[fakes.CONFLICT_409] * times)
    else:
        for _ in range(times):                  # each time, another writer's own (new) run
            world.fake.before_put(DEC, lambda: rewrite(
                world, DEC, add_settled(f"2026-12-05T1000-v1-manual-{next(_seq)}")))


@pytest.mark.parametrize("how", ["409", "412"])
def test_ledger_cas_attempts_counts_tries_in_total(world, how):
    """Change 4. attempts - 1 losses in a row, then a win: the run starts. `attempts`
    losses in a row: ws7_ledger_conflict, and the run does not start."""
    seed(world, "2026-12", [an_entry("2026-12-01T1000-v1-manual", mode="manual")])
    _lose(world, ATTEMPTS - 1, how)
    won = start(world, W1)
    assert len(ledger_puts(world)) == ATTEMPTS                            # the last try won
    assert entry_of(world, won.run_id)["state"] == wl.OPEN
    world.ledger.settle(won, report(won))

    puts = len(ledger_puts(world))
    _lose(world, ATTEMPTS, how)
    world.clock.set("2026-12-09T04:00:30Z")
    held = take(world, W2)
    stop = stopped(lambda: world.ledger.admit(held, R), wl.LEDGER_CONFLICT)
    assert stop.counts == {"attempts": ATTEMPTS}
    assert len(ledger_puts(world)) == puts + ATTEMPTS                     # no try beyond the count
    assert held.run_id not in [e["run_id"] for e in world.fake.json(DEC)["entries"]]


@pytest.mark.parametrize("script,puts", [(fakes.LAND_THEN_500, 1), (fakes.LAND_THEN_DROP, 1),
                                         (fakes.FAIL_500, 2)])
def test_a_ledger_write_whose_reply_failed_is_read_back_and_looked_for_by_its_write_id(world, script,
                                                                                         puts):
    seed(world, "2026-12", [an_entry("2026-12-01T1000-v1-manual", mode="manual")])
    world.fake.script_put(DEC, script)
    admitted = start(world, W1)
    assert len(ledger_puts(world)) == puts                # landed: not written twice; lost: once more
    runs = [e["run_id"] for e in world.fake.json(DEC)["entries"]]
    assert runs.count(admitted.run_id) == 1
    assert admitted.expires_at == store.parse_utc(entry_of(world, admitted.run_id)["expires_at"])


def test_a_landed_write_another_writer_has_since_followed_is_recognised_by_its_run_id(world):
    seed(world, "2026-12", [an_entry("2026-12-01T1000-v1-manual", mode="manual")])
    world.fake.script_put(DEC, fakes.LAND_THEN_500)
    world.fake.after_put(DEC, lambda: rewrite(world, DEC, add_settled("2026-12-05T1000-v1-manual")))
    admitted = start(world, W1)
    assert len(ledger_puts(world)) == 1                                   # not booked a second time
    runs = [e["run_id"] for e in world.fake.json(DEC)["entries"]]
    assert runs.count(admitted.run_id) == 1 and "2026-12-05T1000-v1-manual" in runs


def test_failed_replies_count_towards_the_tries_too(world):
    world.fake.script_put(DEC, *[fakes.FAIL_500] * ATTEMPTS)
    world.clock.set("2026-12-02T04:00:30Z")
    held = take(world, W1)
    stopped(lambda: world.ledger.admit(held, R), wl.LEDGER_CONFLICT)
    assert world.fake.count("PUT", DEC) == ATTEMPTS and DEC not in world.fake.keys()


@pytest.mark.parametrize("break_it,store_code", [
    (lambda w: w.fake.script_get(DEC, 500), store.GET_FAILED),
    (lambda w: w.fake.script_get(DEC, 403), store.GET_FAILED),
    (lambda w: w.fake.script_list(500), store.LIST_FAILED),
    (lambda w: setattr(w.fake, "truncate_listing", True), store.LIST_TRUNCATED),
    (lambda w: w.fake.script_put(DEC, fakes.DENY_403), store.PUT_REFUSED),
])
def test_a_ledger_that_cannot_be_read_or_written_stops_the_run(world, break_it, store_code):
    seed(world, "2026-12", [an_entry("2026-12-01T1000-v1-manual", mode="manual")])
    world.clock.set("2026-12-02T04:00:30Z")
    held = take(world, W1)
    before = world.fake.body(DEC)
    break_it(world)
    stop = stopped(lambda: world.ledger.admit(held, R), wl.LEDGER_UNAVAILABLE)
    assert stop.ids == (store_code,)
    assert world.fake.body(DEC) == before
    world.fake.truncate_listing = False
    assert world.ledger.admit(held, R).run_id == held.run_id              # the partner


def _flow(net, missing_status: int) -> tuple:
    """Two months of ordinary use, with fixed tokens, against a fake that answers a
    GET for a missing key with `missing_status`."""
    w = make_world(net)
    w.fake.missing_status = missing_status
    tokens = (f"{i:032x}" for i in itertools.count(1))
    w.ledger = wl.Ledger(w.store, CFG, clock=w.clock, token=lambda: next(tokens))
    try:
        for when in (W1, W2, "2027-01-06T04:00:00Z"):
            w.clock.set(store.parse_utc(when) + timedelta(seconds=30))
            held = wl.claim(w.store, CFG, target="pipeline", mode="scheduled", scheduled_time=when,
                            clock=w.clock, token=lambda: next(tokens))
            admitted = w.ledger.admit(held, R)
            w.ledger.settle(admitted, report(admitted))
        return (list(w.fake.requests), {k: w.fake.body(k) for k in w.fake.keys()},
                w.fake.missing_gets)
    finally:
        w.fake.stop()


def test_the_code_behaves_the_same_whether_a_missing_key_gives_404_or_403(_offline):
    """Change 3. Absence is decided from the listing, so the status S3 gives a GET for
    a missing key never matters: no such GET is ever made."""
    requests_404, bodies_404, missing_404 = _flow(_offline, 404)
    requests_403, bodies_403, missing_403 = _flow(_offline, 403)
    assert (requests_404, bodies_404) == (requests_403, bodies_403)
    assert missing_404 == missing_403 == 0                                # never asked for a missing key
    assert sorted(bodies_404) == ["ws7/claims/pipeline/2026-12-02", "ws7/claims/pipeline/2026-12-09",
                                  "ws7/claims/pipeline/2027-01-06", DEC, "ws7/ledger/2027-01.json"]
    # A month's first ledger was created, not read: the first request for it is a PUT.
    assert next(m for m, k in requests_404 if k == "ws7/ledger/2027-01.json") == "PUT"


# ------------------------------------------------------- the strict loader

def _good() -> dict:
    return a_ledger("2026-12", [an_entry("2026-12-02T0400-pipeline"),
                                an_entry("2026-12-09T0400-pipeline", wl.UNRESOLVED),
                                an_entry("2026-12-16T0400-pipeline", wl.OPEN)])


def _raw(doc: dict) -> bytes:
    return json.dumps(doc).encode("utf-8")


@pytest.mark.parametrize("damage", [
    lambda d: d.update(note=SENTINEL),                                    # an unknown field
    lambda d: d.pop("assurance_waived"),
    lambda d: d.update(schema="ws7-ledger/2"),
    lambda d: d.update(month="2026-11"),
    lambda d: d.update(rev=0),
    lambda d: d.update(rev=True),
    lambda d: d.update(write_id="short"),
    lambda d: d.update(cap_usd_micros=0),
    lambda d: d.update(assurance_waived="no"),
    lambda d: d.update(entries={}),
    lambda d: d["entries"][0].update(note=SENTINEL),
    lambda d: d["entries"][0].update(run_id="has spaces"),
    lambda d: d["entries"][0].update(target="canary"),
    lambda d: d["entries"][0].update(mode="weekly"),
    lambda d: d["entries"][0].update(state="paid"),
    lambda d: d["entries"][0].update(counted_usd_micros=-1),
    lambda d: d["entries"][0].update(counted_usd_micros=1.5),
    lambda d: d["entries"][0].update(reserved_usd_micros=0),
    lambda d: d["entries"][0].update(opened_at="2026-12-02 04:00"),
    lambda d: d["entries"][0].update(expires_at=d["entries"][0]["opened_at"]),
    lambda d: d["entries"][0].update(basis=None),                         # settled with no basis
    lambda d: d["entries"][0].update(basis=wl.UPPER_BOUND),               # settled on a resolution's basis
    lambda d: d["entries"][0].update(basis=wl.WITHDRAWN),                 # withdrawn, but not at zero
    lambda d: d["entries"][0].update(closed_at=None),
    lambda d: d["entries"][1].update(kind="lost"),
    lambda d: d["entries"][1].update(counted_usd_micros=1),               # abandoned below its reservation
    lambda d: d["entries"][1].update(estimate_usd_micros=None),
    lambda d: d["entries"][1].update(basis=wl.ESTIMATE_ACCEPTED),         # unresolved, with a basis
    lambda d: d["entries"][2].update(counted_usd_micros=1),               # open below its reservation
    lambda d: d["entries"][2].update(kind=wl.CRASHED),
    lambda d: d["entries"].append(dict(d["entries"][0])),                 # one run twice
    lambda d: d["entries"][1].update(state=wl.RESOLVED, basis=wl.ESTIMATE_ACCEPTED,
                                     resolved_at="2026-12-10T09:00:00Z"),  # accepted, month not flagged
])
def test_a_ledger_that_is_not_exactly_the_shape_is_refused(damage):
    assert wl.parse_ledger(_raw(_good()), "2026-12")                      # the partner
    doc = _good()
    damage(doc)
    stop = stopped(lambda: wl.parse_ledger(_raw(doc), "2026-12"), wl.LEDGER_INVALID)
    assert stop.ids == ("2026-12",) and SENTINEL not in stop.line()


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b'{"schema":"ws7-ledger/1","schema":"x"}',
                                 b'{"rev": NaN}', b"\xff\xfe"])
def test_a_ledger_that_is_not_one_json_object_is_refused(raw):
    stopped(lambda: wl.parse_ledger(raw, "2026-12"), wl.LEDGER_INVALID)


def test_an_invalid_ledger_or_a_stray_object_under_the_prefix_stops_the_run(world):
    world.clock.set("2026-12-16T04:00:30Z")
    held = take(world)
    bad = _good()
    bad["note"] = SENTINEL
    world.fake.seed(DEC, _raw(bad))
    stopped(lambda: world.ledger.admit(held, R), wl.LEDGER_INVALID)
    assert world.fake.json(DEC)["note"] == SENTINEL                       # read, refused, not "repaired"

    world.fake.seed(DEC, wl._dump(a_ledger("2026-12", [])))
    world.fake.seed("ws7/ledger/notes.txt", b"invented")
    stopped(lambda: world.ledger.admit(held, R), wl.LEDGER_INVALID)
    world.fake.remove("ws7/ledger/notes.txt")
    assert world.ledger.admit(held, R)                                    # the partner


def test_one_run_in_two_months_ledgers_is_refused(world):
    seed(world, "2026-11", [an_entry("2026-11-25T0400-pipeline")])
    seed(world, "2026-12", [an_entry("2026-11-25T0400-pipeline")])
    stopped(world.ledger.snapshot, wl.LEDGER_INVALID)
    seed(world, "2026-12", [an_entry("2026-12-02T0400-pipeline")])
    assert sorted(world.ledger.snapshot()) == ["2026-11", "2026-12"]


# ------------------------------------------------- what a write may change

def _next(doc: dict, change) -> dict:
    new = json.loads(json.dumps(doc))
    new["rev"] += 1
    change(new)
    return new


@pytest.mark.parametrize("change", [
    lambda d: d.update(assurance_waived=False),                           # the flag cleared
    lambda d: d["entries"].pop(0),                                        # an entry dropped
    lambda d: d["entries"].reverse(),
    lambda d: d.update(rev=d["rev"] + 1),                                 # a revision skipped
    lambda d: d["entries"][0].update(counted_usd_micros=1),               # a settled entry changed
    lambda d: d["entries"][1].update(counted_usd_micros=1),               # a resolved entry changed
    lambda d: d["entries"][1].update(state=wl.UNRESOLVED, basis=None, resolved_at=None),
    lambda d: d["entries"][2].update(reserved_usd_micros=R + 1),          # a reservation changed
    lambda d: d["entries"][2].update(expires_at="2026-12-31T00:00:00Z"),  # an expiry moved
    lambda d: d["entries"][3].update(state=wl.RESOLVED, basis=wl.UPPER_BOUND,
                                     resolved_at="2026-12-20T09:00:00Z"),  # open straight to resolved
    lambda d: d["entries"].append(an_entry("2026-12-30T0400-pipeline")),  # a new entry, not open
])
def test_a_write_the_rules_do_not_allow_is_refused_before_it_is_sent(change):
    old = a_ledger("2026-12", waived=True, rev=7, entries=[
        an_entry("2026-12-02T0400-pipeline"),
        an_entry("2026-12-09T0400-pipeline", wl.RESOLVED, basis=wl.ESTIMATE_ACCEPTED, counted=R),
        an_entry("2026-12-16T0400-pipeline", wl.UNRESOLVED),
        an_entry("2026-12-23T0400-pipeline", wl.OPEN)])
    # The partners: the moves the rules do allow.
    wl._check_transition(old, _next(old, lambda d: None))
    wl._check_transition(old, _next(old, lambda d: d["entries"][3].update(
        state=wl.SETTLED, basis=wl.METERED, counted_usd_micros=5, closed_at="2026-12-23T04:40:00Z")))
    wl._check_transition(old, _next(old, lambda d: d["entries"][2].update(
        state=wl.RESOLVED, basis=wl.UPPER_BOUND, counted_usd_micros=5,
        resolved_at="2026-12-20T09:00:00Z")))
    wl._check_transition(old, _next(old, lambda d: d["entries"].append(
        an_entry("2026-12-30T0400-pipeline", wl.OPEN))))
    stopped(lambda: wl._check_transition(old, _next(old, change)), wl.LEDGER_WRITE_INVALID)


def test_a_new_months_ledger_starts_at_revision_one():
    first = a_ledger("2026-12", [an_entry("2026-12-02T0400-pipeline", wl.OPEN)])
    wl._check_transition(None, first)
    stopped(lambda: wl._check_transition(None, dict(first, rev=2)), wl.LEDGER_WRITE_INVALID)


# =============================================================== admission ==

def test_a_run_is_admitted_when_the_total_plus_its_reservation_equals_the_cap(world):
    seed(world, "2026-12", [an_entry("2026-12-02T0400-pipeline", counted=CAP - R)])
    admitted = start(world, W2)
    doc = world.fake.json(DEC)
    assert wl.month_total(doc) == CAP                                     # exactly the cap, admitted
    assert entry_of(world, admitted.run_id) == {
        "run_id": "2026-12-09T0400-pipeline", "target": "pipeline", "mode": "scheduled",
        "state": "open", "kind": None, "basis": None, "reserved_usd_micros": R,
        "counted_usd_micros": R, "estimate_usd_micros": None, "opened_at": "2026-12-09T04:00:30Z",
        "expires_at": "2026-12-09T06:00:30Z", "closed_at": None, "resolved_at": None}


def test_a_run_is_refused_when_the_total_plus_its_reservation_is_one_micro_dollar_over(world):
    seed(world, "2026-12", [an_entry("2026-12-02T0400-pipeline", counted=CAP - R + 1)])
    before = world.fake.body(DEC)
    world.clock.set("2026-12-09T04:00:30Z")
    held = take(world, W2)
    stop = stopped(lambda: world.ledger.admit(held, R), wl.OVER_CAP)
    assert stop.ids == ("2026-12",)
    assert stop.counts == {"month_total_usd_micros": CAP - R + 1, "cap_usd_micros": CAP,
                           "reservation_usd_micros": R}
    assert world.fake.body(DEC) == before and ledger_puts(world) == []    # nothing reserved


def test_the_cap_is_per_month_and_an_open_reservation_counts_in_full(world):
    seed(world, "2026-11", [an_entry("2026-11-25T0400-pipeline", counted=CAP)])
    first = start(world, W1)                                              # November is full; December is not
    assert wl.month_total(world.fake.json(DEC)) == R                      # open: counted at its reservation
    world.ledger.settle(first, report(first, total_nanos=1_903_210_001))
    assert wl.month_total(world.fake.json(DEC)) == 1_903_211              # settled: rounded up


def test_a_manual_run_is_outside_the_cap_neither_checked_nor_counted(world):
    seed(world, "2026-12", [an_entry("2026-12-02T0400-pipeline", counted=CAP - R)])
    baseline = reserve.reservation("pipeline", 55, MARGIN)["total_usd_micros"]
    assert baseline > CAP                                                 # it could never fit inside
    manual = start(world, "2026-12-05T10:00:00Z", mode="manual", reservation=baseline)
    assert entry_of(world, manual.run_id)["reserved_usd_micros"] == baseline
    assert wl.month_total(world.fake.json(DEC)) == CAP - R                # not counted while open
    world.ledger.settle(manual, report(manual, total_nanos=9_000_000_000))
    assert wl.month_total(world.fake.json(DEC)) == CAP - R                # nor once settled
    assert start(world, W2).run_id == "2026-12-09T0400-pipeline"          # still exactly fits
    assert wl.month_total(world.fake.json(DEC)) == CAP


@pytest.mark.parametrize("second_mode", ["scheduled", "manual"])
def test_an_open_reservation_refuses_any_other_run(world, second_mode):
    first = start(world, W1)
    world.clock.set("2026-12-02T04:30:30Z")
    held = take(world, "2026-12-02T04:30:00Z", target="v1", mode=second_mode)
    before = world.fake.body(DEC)
    stop = stopped(lambda: world.ledger.admit(held, RES["v1"]), wl.RUN_IN_PROGRESS)
    assert stop.ids == (first.run_id,)
    assert world.fake.body(DEC) == before
    world.ledger.settle(first, report(first))                           # the partner: it has ended
    assert world.ledger.admit(held, RES["v1"]).run_id == held.run_id


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "8280476", None])
def test_a_reservation_is_a_whole_number_of_micro_dollars_above_zero(world, bad):
    world.clock.set("2026-12-16T04:00:30Z")
    held = take(world)
    stopped(lambda: world.ledger.admit(held, bad), wl.RESERVATION_INVALID)
    assert ledger_puts(world) == []
    assert world.ledger.admit(held, 1).reservation_usd_micros == 1        # the partner


def test_one_claim_books_one_entry(world):
    world.clock.set("2026-12-16T04:00:30Z")
    held = take(world)
    world.ledger.admit(held, R)
    stop = stopped(lambda: world.ledger.admit(held, R), wl.ENTRY_EXISTS)
    assert stop.ids == (held.run_id,)
    with pytest.raises(TypeError):
        world.ledger.admit(held.run_id, R)


def test_a_run_booked_in_another_months_ledger_at_the_same_moment_makes_this_one_withdraw(world):
    """No single compare-and-swap sees two objects. After its entry is written the run
    reads every month again; finding another live run, it withdraws at zero."""
    other = an_entry("2026-11-30T2359-pipeline-manual", wl.OPEN, mode="manual",
                     opened="2026-12-02T04:00:29Z", expires="2026-12-02T09:00:29Z")
    world.fake.after_put(DEC, lambda: seed(world, "2026-11", [other]))
    world.clock.set("2026-12-02T04:00:30Z")
    held = take(world, W1)
    stop = stopped(lambda: world.ledger.admit(held, R), wl.RUN_IN_PROGRESS)
    assert stop.ids == ("2026-11-30T2359-pipeline-manual",)
    mine = entry_of(world, held.run_id)
    assert (mine["state"], mine["basis"], mine["counted_usd_micros"]) == (wl.SETTLED, wl.WITHDRAWN, 0)
    assert wl.month_total(world.fake.json(DEC)) == 0

    # The partner: with no other run, the same admission starts.
    world.fake.remove(NOV)
    assert start(world, W2).run_id == "2026-12-09T0400-pipeline"


# -------------------------------------------- the deadline and expires_at

def test_admission_returns_the_runs_deadline_ahead_of_expires_at(world):
    """Change 7. The guard's deadline ends DEADLINE_MARGIN_S before another task may
    mark the entry abandoned."""
    scheduled = start(world, W1)
    assert scheduled.opened_at == store.parse_utc("2026-12-02T04:00:30Z")
    assert scheduled.expires_at - scheduled.opened_at \
        == timedelta(seconds=CFG.wall_clock_limit_scheduled_s)
    assert scheduled.expires_at - scheduled.deadline_at == timedelta(seconds=wl.DEADLINE_MARGIN_S)
    assert scheduled.seconds_to_deadline(world.clock()) \
        == CFG.wall_clock_limit_scheduled_s - wl.DEADLINE_MARGIN_S
    world.ledger.settle(scheduled, report(scheduled))

    manual = start(world, "2026-12-05T10:00:00Z", mode="manual")
    assert manual.expires_at - manual.opened_at == timedelta(seconds=CFG.wall_clock_limit_manual_s)
    assert manual.expires_at - manual.deadline_at == timedelta(seconds=wl.DEADLINE_MARGIN_S)


def test_the_margin_covers_one_call_in_flight_for_both_targets():
    """At least 120 s: v1's 60 s to connect plus 60 s to read. Read from the plans and
    the runner, so a longer timeout there fails here."""
    v1, pipeline = reserve.plan("v1"), reserve.plan("pipeline")
    assert v1.connect_timeout_s + v1.read_timeout_s == 120
    longest_pipeline = pipeline.connect_timeout_s + max(rpe.READ_TIMEOUT_S, rpe.READ_TIMEOUT_5A_S)
    assert wl.DEADLINE_MARGIN_S >= max(120, v1.connect_timeout_s + v1.read_timeout_s,
                                       longest_pipeline)


def test_a_wall_clock_limit_no_longer_than_the_margin_is_refused(_offline):
    short = dataclasses.replace(CFG, wall_clock_limit_scheduled_s=wl.DEADLINE_MARGIN_S)
    w = make_world(_offline, short)
    try:
        w.clock.set("2026-12-16T04:00:30Z")
        held = take(w)
        stopped(lambda: w.ledger.admit(held, R), wl.WALL_CLOCK_LIMIT_TOO_SHORT)
        assert ledger_puts(w) == []
        longer = dataclasses.replace(CFG, wall_clock_limit_scheduled_s=wl.DEADLINE_MARGIN_S + 1)
        admitted = wl.Ledger(w.store, longer, clock=w.clock).admit(held, R)      # the partner
        assert admitted.deadline_at - admitted.opened_at == timedelta(seconds=1)
    finally:
        w.fake.stop()


def test_one_second_before_expires_at_the_run_is_in_progress_and_at_expires_at_it_is_abandoned(world):
    """Change 6, with the injected clock."""
    first = start(world, W2)
    assert first.expires_at == store.parse_utc("2026-12-09T06:00:30Z")

    world.clock.set(first.expires_at - timedelta(seconds=1))
    early = take(world, "2026-12-09T05:30:00Z", mode="manual")
    stop = stopped(lambda: world.ledger.admit(early, R), wl.RUN_IN_PROGRESS)
    assert stop.ids == (first.run_id,)
    assert entry_of(world, first.run_id)["state"] == wl.OPEN              # still the run's own

    world.clock.set(first.expires_at)
    admitted = world.ledger.admit(early, R)                               # the same claim, one second on
    assert admitted.run_id == "2026-12-09T0530-pipeline-manual"
    gone = entry_of(world, first.run_id)
    assert (gone["state"], gone["kind"], gone["basis"]) == (wl.UNRESOLVED, wl.ABANDONED, None)
    assert gone["counted_usd_micros"] == gone["estimate_usd_micros"] == R  # at its reservation
    assert gone["closed_at"] == "2026-12-09T06:00:30Z"


def test_at_expires_at_a_scheduled_run_finds_the_entry_abandoned_and_is_blocked_by_it(world):
    first = start(world, W2)
    world.clock.set(first.expires_at)
    held = take(world, "2026-12-09T05:30:00Z", target="v1")
    stop = stopped(lambda: world.ledger.admit(held, RES["v1"]), wl.BLOCKED_UNRESOLVED)
    assert stop.ids == (first.run_id,)
    assert entry_of(world, first.run_id)["kind"] == wl.ABANDONED          # marked, then it blocks


def test_a_late_settle_on_an_abandoned_entry_is_refused_and_the_entry_stays_unresolved(world):
    """Change 6. Only resolve() moves an abandoned entry on."""
    first = start(world, W2)
    world.clock.set(first.expires_at)
    world.ledger.expire()
    marked = entry_of(world, first.run_id)
    assert marked["kind"] == wl.ABANDONED

    world.clock.set(first.expires_at + timedelta(seconds=5))
    stop = stopped(lambda: world.ledger.settle(first, report(first)), wl.ENTRY_NOT_OPEN)
    assert stop.ids == (first.run_id,)
    assert entry_of(world, first.run_id) == marked                        # unchanged, still unresolved
    stopped(lambda: world.ledger.mark_crashed(first), wl.ENTRY_NOT_OPEN)
    assert entry_of(world, first.run_id) == marked


def test_a_settle_on_an_open_entry_succeeds_up_to_one_second_before_expires_at(world):
    """The partner of the late settle."""
    first = start(world, W2)
    world.clock.set(first.expires_at - timedelta(seconds=1))
    settled = world.ledger.settle(first, report(first))
    assert (settled["state"], settled["basis"], settled["counted_usd_micros"]) \
        == (wl.SETTLED, wl.METERED, 1_903_211)
    assert settled["closed_at"] == "2026-12-09T06:00:29Z"
    assert entry_of(world, first.run_id) == settled


def test_a_settle_that_arrives_at_expires_at_is_refused_and_marks_the_entry_abandoned(world):
    """Nobody else has looked yet: the late settle itself finds the entry expired."""
    first = start(world, W2)
    world.clock.set(first.expires_at)
    stopped(lambda: world.ledger.settle(first, report(first)), wl.ENTRY_NOT_OPEN)
    assert entry_of(world, first.run_id)["kind"] == wl.ABANDONED


# ================================================================ settling ==

class _Inner:
    """A strict in-process stand-in for the Bedrock client: scripted, and it raises
    on anything unexpected."""

    def __init__(self, *replies):
        self.replies = list(replies)

    def converse(self, **_request):
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _guard_report(admitted: wl.Admission, second_call) -> tuple:
    """A real Guard, one v1 generation in a batch labelled as part 5 will label it:
    the first call answers with invented usage, the second does `second_call`.
    Returns the report and the two costs."""
    plan = reserve.plan("v1")
    booked = reserve.reservation("v1", 19, MARGIN)
    guard = wc.Guard(plan, reservation_nanos=booked["total_usd_nanos"], margin_percent=MARGIN,
                     deadline=time.monotonic() + 60)
    usage = {"inputTokens": 5000, "outputTokens": 900}                    # invented
    client = wc.GuardedClient(_Inner({"usage": usage}, second_call), guard)
    guard.begin_batch(f"{admitted.run_id}.main", 1)
    for step in plan.steps:
        request = {"modelId": plan.model_id, "inferenceConfig": {"maxTokens": step.max_tokens},
                   "messages": [{"role": "user", "content": [{"text": SENTINEL}]}]}
        try:
            client.converse(**request)
        except ClientError:
            pass
    guard.end_batch()
    return (guard.report(), reserve.read_usage(usage)["cost_usd_nanos"],
            reserve.worst_case_nanos(plan.steps[1], MARGIN))


def _error(code: str, status: int) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "invented"},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, "Converse")


def test_settling_uses_the_guards_total_usage_where_a_reply_was_seen_worst_case_where_not(world):
    """The first call replied (its usage is its cost). The second was throttled: no
    reply, the model did not run, so it is counted at its worst case. Metered."""
    admitted = start(world, W1, target="v1")
    guard_report, seen, worst = _guard_report(admitted, _error("ThrottlingException", 429))
    assert guard_report["total_usd_nanos"] == seen + worst and not guard_report["unmetered"]
    assert list(guard_report["batches"]) == ["2026-12-02T0400-v1.main"]
    settled = world.ledger.settle(admitted, guard_report)
    assert (settled["state"], settled["basis"], settled["kind"]) == (wl.SETTLED, wl.METERED, None)
    assert settled["counted_usd_micros"] == reserve.to_micros(seen + worst) < RES["v1"]
    assert settled["estimate_usd_micros"] is None and settled["closed_at"] == "2026-12-02T04:00:30Z"
    assert SENTINEL not in world.fake.body(DEC).decode("utf-8")


def test_an_unmetered_run_is_settled_at_its_counted_total_and_is_unresolved(world):
    """Ruling on reading 4: the guard's counted total, not the reservation."""
    admitted = start(world, W1, target="v1")
    guard_report, seen, worst = _guard_report(admitted, _error("InternalServerException", 500))
    assert guard_report["unmetered"] is True
    entry = world.ledger.settle(admitted, guard_report)
    assert (entry["state"], entry["kind"], entry["basis"]) == (wl.UNRESOLVED, wl.UNMETERED, None)
    assert entry["counted_usd_micros"] == entry["estimate_usd_micros"] \
        == reserve.to_micros(seen + worst)
    assert entry["counted_usd_micros"] < entry["reserved_usd_micros"] == RES["v1"]
    assert world.ledger.unresolved() == [("2026-12", admitted.run_id, wl.UNMETERED)]


def test_an_abandoned_and_a_crashed_entry_count_at_the_reservation(world):
    crashed = start(world, W1)
    entry = world.ledger.mark_crashed(crashed)
    assert (entry["state"], entry["kind"]) == (wl.UNRESOLVED, wl.CRASHED)
    assert entry["counted_usd_micros"] == entry["estimate_usd_micros"] == R
    abandoned = start(world, "2026-12-05T10:00:00Z", mode="manual")
    world.clock.set(abandoned.expires_at)
    world.ledger.expire()
    entry = entry_of(world, abandoned.run_id)
    assert (entry["kind"], entry["counted_usd_micros"], entry["estimate_usd_micros"]) \
        == (wl.ABANDONED, R, R)


@pytest.mark.parametrize("change", [
    {"target": "v1"},                                                     # another target's report
    {"reservation_usd_nanos": 1},                                         # another run's reservation
    {"total_usd_micros": 1},                                              # micros that are not the nanos
    {"total_usd_nanos": -1}, {"total_usd_nanos": 1.5}, {"total_usd_nanos": True},
    {"unmetered": "no"}, {"unmetered": None}, {"target": "canary"}, {"target": None},
    {"reservation_usd_nanos": 0},
    {"batches": None}, {"batches": ["2026-12-02T0400-pipeline.main"]},
    {"batches": {"b": {}}},                                               # not labelled for any run
    {"batches": {"2026-12-09T0400-pipeline.main": {}}},                   # another run's batch
    {"batches": {"2026-12-02T0400-pipeline.main": {}, "2026-12-09T0400-pipeline.main": {}}},
    {"batches": {"2026-12-02T0400-pipeline": {}}},                        # the run ID with no ".<name>"
    {"batches": {"2026-12-02T0400-pipeline.": {}}},
    {"batches": {"2026-12-02T0400-pipeline-manual.main": {}}},            # a run whose ID merely starts the same
    {"batches": {7: {}}},
])
def test_a_report_that_is_not_this_runs_own_is_refused_and_the_entry_stays_open(world, change):
    admitted = start(world, W1)
    before = world.fake.body(DEC)
    stop = stopped(lambda: world.ledger.settle(admitted, dict(report(admitted), **change)),
                   wl.REPORT_INVALID)
    assert stop.ids == (admitted.run_id,)
    stopped(lambda: world.ledger.settle(admitted, "a string"), wl.REPORT_INVALID)
    assert world.fake.body(DEC) == before and len(ledger_puts(world)) == 1   # refused before any write
    assert world.ledger.settle(admitted, report(admitted))["state"] == wl.SETTLED   # the partner


def test_a_report_whose_micro_dollar_total_is_a_boolean_is_refused(world):
    """In Python True == 1, so a `total_usd_micros` of True compares equal to a
    nano-dollar total that rounds up to one micro-dollar. A boolean is not a count:
    the report is refused, and the entry stays open."""
    admitted = start(world, W1)
    before = world.fake.body(DEC)
    one = report(admitted, total_nanos=1)                                 # rounds up to 1 micro-dollar
    assert one["total_usd_micros"] == 1 and type(one["total_usd_micros"]) is int
    as_true = dict(one, total_usd_micros=True)
    assert as_true["total_usd_micros"] == reserve.to_micros(as_true["total_usd_nanos"])   # why it could slip by
    stop = stopped(lambda: world.ledger.settle(admitted, as_true), wl.REPORT_INVALID)
    assert stop.code == "ws7_report_invalid"
    assert world.fake.body(DEC) == before and entry_of(world, admitted.run_id)["state"] == wl.OPEN

    settled = world.ledger.settle(admitted, one)                          # the partner: the integer 1
    assert (settled["state"], settled["basis"]) == (wl.SETTLED, wl.METERED)
    assert settled["counted_usd_micros"] == 1 and type(settled["counted_usd_micros"]) is int


def test_a_settled_entry_never_changes_again(world):
    admitted = start(world, W1)
    settled = world.ledger.settle(admitted, report(admitted))
    stopped(lambda: world.ledger.settle(admitted, report(admitted, total_nanos=1)), wl.ENTRY_NOT_OPEN)
    stopped(lambda: world.ledger.mark_crashed(admitted), wl.ENTRY_NOT_OPEN)
    stopped(lambda: world.ledger.resolve(admitted.run_id, wl.UPPER_BOUND, 1), wl.ENTRY_NOT_UNRESOLVED)
    assert entry_of(world, admitted.run_id) == settled
    stranger = dataclasses.replace(admitted, run_id="2026-12-30T0400-pipeline")
    stopped(lambda: world.ledger.settle(stranger, report(stranger)), wl.ENTRY_UNKNOWN)
    stopped(lambda: world.ledger.mark_crashed(stranger), wl.ENTRY_UNKNOWN)


def test_a_settle_whose_reply_failed_is_not_applied_twice(world):
    admitted = start(world, W1)
    world.fake.script_put(DEC, fakes.LAND_THEN_500)
    world.fake.after_put(DEC, lambda: rewrite(world, DEC, add_settled("2026-12-05T1000-v1-manual")))
    settled = world.ledger.settle(admitted, report(admitted))
    assert settled["state"] == wl.SETTLED and entry_of(world, admitted.run_id) == settled
    assert len(ledger_puts(world)) == 2                                   # the booking, and one settle


# ====================================== unresolved: each case, and the block ==

def leave_unmetered(world, when: str, mode: str = "scheduled") -> str:
    admitted = start(world, when, mode=mode)
    world.ledger.settle(admitted, report(admitted, total_nanos=2_000_000_000, unmetered=True))
    return admitted.run_id


def leave_abandoned(world, when: str, mode: str = "scheduled") -> str:
    """The run died outright: it reported nothing, and its entry is found at expiry."""
    admitted = start(world, when, mode=mode)
    world.clock.set(admitted.expires_at)
    world.ledger.expire()
    return admitted.run_id


def leave_crashed(world, when: str, mode: str = "scheduled") -> str:
    admitted = start(world, when, mode=mode)
    world.ledger.mark_crashed(admitted)
    return admitted.run_id


LEAVE = {wl.UNMETERED: leave_unmetered, wl.ABANDONED: leave_abandoned, wl.CRASHED: leave_crashed}
ESTIMATE = {wl.UNMETERED: 2_000_000, wl.ABANDONED: R, wl.CRASHED: R}
KIND = pytest.mark.parametrize("kind", list(LEAVE))


@KIND
def test_each_unresolved_case_blocks_the_next_scheduled_run_which_reserves_nothing(world, kind):
    blocker = LEAVE[kind](world, W1)
    assert entry_of(world, blocker)["kind"] == kind
    world.clock.set("2026-12-09T04:00:30Z")
    held = take(world, W2)
    before, puts = ledger_bodies(world), len(ledger_puts(world))

    stop = stopped(lambda: world.ledger.admit(held, R), wl.BLOCKED_UNRESOLVED)
    assert stop.ids == (blocker,) and stop.counts == {"unresolved": 1}
    assert stop.line() == f"ws7 stop code=ws7_blocked_unresolved ids={blocker} unresolved=1"
    assert ledger_bodies(world) == before and len(ledger_puts(world)) == puts   # nothing reserved
    assert world.ledger.unresolved() == [("2026-12", blocker, kind)]


@KIND
@pytest.mark.parametrize("basis", wl.RESOLUTION_BASES)
def test_each_case_resolves_on_both_bases_and_then_stops_blocking(world, kind, basis):
    blocker = LEAVE[kind](world, W1)
    figure = ESTIMATE[kind] if basis == wl.ESTIMATE_ACCEPTED else 3_141_592
    world.clock.set("2026-12-04T09:00:00Z")
    entry = world.ledger.resolve(blocker, basis, figure)
    assert (entry["state"], entry["kind"], entry["basis"]) == (wl.RESOLVED, kind, basis)   # stored as given
    assert entry["counted_usd_micros"] == figure and entry["resolved_at"] == "2026-12-04T09:00:00Z"
    assert entry["estimate_usd_micros"] == ESTIMATE[kind]                 # what it was resolved from
    assert entry_of(world, blocker) == entry
    assert world.ledger.unresolved() == []
    assert start(world, W2).run_id == "2026-12-09T0400-pipeline"          # no longer blocked
    assert wl.month_total(world.fake.json(DEC)) == figure + R             # it counts at the settled figure


def test_a_resolved_entry_does_not_block_where_the_same_entry_unresolved_did(world):
    """The partner of the block, on one entry: blocked, resolved, admitted."""
    blocker = leave_unmetered(world, W1)
    world.clock.set("2026-12-09T04:00:30Z")
    held = take(world, W2)
    stopped(lambda: world.ledger.admit(held, R), wl.BLOCKED_UNRESOLVED)
    world.ledger.resolve(blocker, wl.UPPER_BOUND, 2_500_000)
    assert world.ledger.admit(held, R).run_id == held.run_id


@KIND
def test_an_entry_from_the_month_before_still_blocks(world, kind):
    blocker = LEAVE[kind](world, "2026-11-25T04:00:00Z")
    world.clock.set("2026-12-02T04:00:30Z")
    held = take(world, W1)
    stop = stopped(lambda: world.ledger.admit(held, R), wl.BLOCKED_UNRESOLVED)
    assert stop.ids == (blocker,)
    assert DEC not in world.fake.keys()                                   # December's ledger not even made
    world.ledger.resolve(blocker, wl.UPPER_BOUND, 4_000_000)              # the partner
    assert world.ledger.admit(held, R).month == "2026-12"
    assert wl.month_total(world.fake.json(DEC)) == R                      # November's figure is November's


def test_an_earlier_months_entry_blocks_when_the_runs_own_month_already_has_a_ledger(world):
    """The block is found by reading every month, not the newest ledger or the run's
    own. November's entry is unresolved; December already has a ledger, written by a
    manual run that came and went."""
    blocker = leave_unmetered(world, "2026-11-25T04:00:00Z")
    manual = start(world, "2026-12-01T10:00:00Z", mode="manual")
    world.ledger.settle(manual, report(manual))
    assert sorted(world.ledger.snapshot()) == ["2026-11", "2026-12"]      # both months have a ledger
    world.clock.set("2026-12-02T04:00:30Z")
    held = take(world, W1)
    before, puts = ledger_bodies(world), len(ledger_puts(world))
    stop = stopped(lambda: world.ledger.admit(held, R), wl.BLOCKED_UNRESOLVED)
    assert stop.ids == (blocker,)
    assert ledger_bodies(world) == before and len(ledger_puts(world)) == puts   # nothing reserved

    world.ledger.resolve(blocker, wl.UPPER_BOUND, 4_000_000)              # the partner: once resolved
    assert world.ledger.admit(held, R).month == "2026-12"


def test_an_entry_two_months_back_with_no_ledger_between_still_blocks(world):
    """Change 5. December's entry, no January ledger at all, a February run."""
    blocker = leave_abandoned(world, W5)
    world.clock.set("2027-02-03T04:00:30Z")
    held = take(world, "2027-02-03T04:00:00Z")
    assert [k for k in world.fake.keys() if store.ledger_month(k)] == [DEC]      # the gap is real
    stop = stopped(lambda: world.ledger.admit(held, R), wl.BLOCKED_UNRESOLVED)
    assert stop.ids == (blocker,)
    assert "ws7/ledger/2027-02.json" not in world.fake.keys()

    world.ledger.resolve(blocker, wl.UPPER_BOUND, 6_000_000)              # the partner: once resolved
    assert world.ledger.admit(held, R).month == "2027-02"
    assert sorted(world.ledger.snapshot()) == ["2026-12", "2027-02"]      # still no January


@KIND
def test_a_manual_run_is_not_blocked_by_an_unresolved_entry(world, kind):
    blocker = LEAVE[kind](world, W1)
    manual = start(world, "2026-12-05T10:00:00Z", mode="manual")
    assert manual.run_id == "2026-12-05T1000-pipeline-manual"
    assert entry_of(world, blocker)["state"] == wl.UNRESOLVED             # still unresolved
    world.ledger.settle(manual, report(manual))
    world.clock.set("2026-12-09T04:00:30Z")                               # the partner: scheduled is blocked
    held = take(world, W2)
    stopped(lambda: world.ledger.admit(held, R), wl.BLOCKED_UNRESOLVED)


def test_an_unresolved_outside_cap_entry_blocks_scheduled_runs_too(world):
    """Reading 5: any unresolved entry blocks, a manual run's included."""
    blocker = leave_unmetered(world, "2026-12-05T10:00:00Z", mode="manual")
    assert entry_of(world, blocker)["mode"] == "manual"
    world.clock.set("2026-12-09T04:00:30Z")
    held = take(world, W2)
    stopped(lambda: world.ledger.admit(held, R), wl.BLOCKED_UNRESOLVED)
    world.ledger.resolve(blocker, wl.ESTIMATE_ACCEPTED, 2_000_000)
    assert world.fake.json(DEC)["assurance_waived"] is True               # and its acceptance flags the month
    assert world.ledger.admit(held, R)
    assert wl.month_total(world.fake.json(DEC)) == R                      # a manual figure is never counted


@pytest.mark.parametrize("basis", ["budget", "", None, "UPPER_BOUND", "metered", "withdrawn",
                                   ["upper_bound"], f"upper_bound {SENTINEL}"])
def test_an_unknown_basis_is_refused_and_the_entry_stays_unresolved(world, basis):
    blocker = leave_crashed(world, W1)
    before = world.fake.body(DEC)
    stop = stopped(lambda: world.ledger.resolve(blocker, basis, R), wl.BASIS_UNKNOWN)
    assert SENTINEL not in stop.line() and world.fake.body(DEC) == before
    assert world.ledger.resolve(blocker, wl.UPPER_BOUND, R)["basis"] == wl.UPPER_BOUND   # the partner


@pytest.mark.parametrize("figure", [-1, True, 1.5, "5000000", None])
def test_a_settled_figure_is_a_whole_number_of_micro_dollars(world, figure):
    blocker = leave_crashed(world, W1)
    stopped(lambda: world.ledger.resolve(blocker, wl.UPPER_BOUND, figure), wl.SETTLED_FIGURE_INVALID)
    assert world.ledger.resolve(blocker, wl.UPPER_BOUND, 0)["counted_usd_micros"] == 0   # zero is one


@KIND
def test_estimate_accepted_must_be_the_entrys_own_estimate(world, kind):
    """Reading 6. Another figure under that label is refused."""
    blocker = LEAVE[kind](world, W1)
    stop = stopped(lambda: world.ledger.resolve(blocker, wl.ESTIMATE_ACCEPTED, ESTIMATE[kind] - 1),
                   wl.ESTIMATE_DIFFERS)
    assert stop.ids == (blocker,)
    assert world.fake.json(DEC)["assurance_waived"] is False
    assert world.ledger.resolve(blocker, wl.ESTIMATE_ACCEPTED, ESTIMATE[kind])["basis"] \
        == wl.ESTIMATE_ACCEPTED


def test_only_an_unresolved_entry_can_be_resolved_and_only_once(world):
    blocker = leave_crashed(world, W1)
    stopped(lambda: world.ledger.resolve("2026-12-30T0400-pipeline", wl.UPPER_BOUND, 1),
            wl.ENTRY_UNKNOWN)
    resolved = world.ledger.resolve(blocker, wl.UPPER_BOUND, 5_000_000)
    stopped(lambda: world.ledger.resolve(blocker, wl.UPPER_BOUND, 1), wl.ENTRY_NOT_UNRESOLVED)
    stopped(lambda: world.ledger.resolve(blocker, wl.ESTIMATE_ACCEPTED, R), wl.ENTRY_NOT_UNRESOLVED)
    assert entry_of(world, blocker) == resolved
    assert world.fake.json(DEC)["assurance_waived"] is False

    still_open = start(world, W2)
    stopped(lambda: world.ledger.resolve(still_open.run_id, wl.UPPER_BOUND, 1),
            wl.ENTRY_NOT_UNRESOLVED)


def test_an_entry_past_its_expiry_can_be_resolved_in_one_step(world):
    """Nobody has marked it yet: resolve() finds it expired, marks it, and resolves it."""
    admitted = start(world, W1)
    world.clock.set(admitted.expires_at + timedelta(hours=30))
    entry = world.ledger.resolve(admitted.run_id, wl.UPPER_BOUND, 7_000_000)
    assert (entry["state"], entry["kind"], entry["basis"]) == (wl.RESOLVED, wl.ABANDONED,
                                                               wl.UPPER_BOUND)


# ----------------------------------------------- crashed, abandoned, both

def test_a_crashed_run_blocks_at_once_without_waiting_for_its_expiry(world):
    crashed = start(world, W1)
    world.clock.set("2026-12-02T04:10:00Z")                               # long before expires_at
    world.ledger.mark_crashed(crashed)
    held = take(world, "2026-12-02T04:05:00Z", target="v1")
    stop = stopped(lambda: world.ledger.admit(held, RES["v1"]), wl.BLOCKED_UNRESOLVED)
    assert stop.ids == (crashed.run_id,)


def test_the_overlap_a_run_that_dies_before_settling_is_found_at_expiry_and_takes_one_kind(world):
    """A run killed outright cannot report, so nothing marks it crashed: it is the
    abandoned case. And a run that did report is not relabelled at its expiry."""
    silent = start(world, W1)                                             # dies; says nothing
    world.clock.set(silent.expires_at - timedelta(seconds=1))
    world.ledger.expire()
    assert entry_of(world, silent.run_id)["state"] == wl.OPEN             # not yet: still its own
    world.clock.set(silent.expires_at)
    world.ledger.expire()
    found = entry_of(world, silent.run_id)
    assert (found["state"], found["kind"]) == (wl.UNRESOLVED, wl.ABANDONED)
    world.ledger.resolve(silent.run_id, wl.UPPER_BOUND, 1)

    reported = start(world, W2)                                           # dies, but reports first
    world.ledger.mark_crashed(reported)
    world.clock.set(reported.expires_at + timedelta(hours=1))
    world.ledger.expire()
    assert entry_of(world, reported.run_id)["kind"] == wl.CRASHED         # the first label stands
    assert world.ledger.unresolved() == [("2026-12", reported.run_id, wl.CRASHED)]


def test_a_crash_report_that_arrives_after_expiry_is_refused_and_the_entry_is_abandoned(world):
    admitted = start(world, W1)
    world.clock.set(admitted.expires_at)
    stopped(lambda: world.ledger.mark_crashed(admitted), wl.ENTRY_NOT_OPEN)
    assert entry_of(world, admitted.run_id)["kind"] == wl.ABANDONED


# ============================================ the "assurance waived" flag ==

@KIND
def test_estimate_accepted_sets_the_months_flag_and_upper_bound_does_not(world, kind):
    first = LEAVE[kind](world, W1)
    world.ledger.resolve(first, wl.UPPER_BOUND, 3_000_000)
    assert world.fake.json(DEC)["assurance_waived"] is False              # the partner
    assert world.ledger.assurance_waived("2026-12") is False

    second = LEAVE[kind](world, W2)
    world.ledger.resolve(second, wl.ESTIMATE_ACCEPTED, ESTIMATE[kind])
    assert world.fake.json(DEC)["assurance_waived"] is True
    assert world.ledger.assurance_waived("2026-12") is True
    assert world.ledger.assurance_waived("2027-01") is False              # a month with no ledger


def test_the_flag_is_set_in_the_entrys_own_month_not_the_month_it_was_resolved_in(world):
    blocker = leave_unmetered(world, "2026-11-25T04:00:00Z")
    first = start(world, "2026-12-01T10:00:00Z", mode="manual")           # December has a ledger
    world.ledger.settle(first, report(first))
    world.clock.set("2026-12-04T09:00:00Z")
    world.ledger.resolve(blocker, wl.ESTIMATE_ACCEPTED, 2_000_000)
    assert world.fake.json(NOV)["assurance_waived"] is True
    assert world.fake.json(DEC)["assurance_waived"] is False


def test_the_flag_stays_set_through_every_later_write_to_that_months_ledger(world):
    waived = leave_unmetered(world, W1)
    world.ledger.resolve(waived, wl.ESTIMATE_ACCEPTED, 2_000_000)
    revision = world.fake.json(DEC)["rev"]

    def still_set(what: str) -> None:
        nonlocal revision
        doc = world.fake.json(DEC)
        assert doc["assurance_waived"] is True, f"the flag was cleared by {what}"
        assert doc["rev"] > revision, f"{what} did not write"            # the control: a write happened
        revision = doc["rev"]

    manual = start(world, "2026-12-05T10:00:00Z", mode="manual")
    still_set("a manual admission")
    world.ledger.settle(manual, report(manual))
    still_set("a settle")
    crashed = start(world, W2)
    still_set("a scheduled admission")
    world.ledger.mark_crashed(crashed)
    still_set("a crash mark")
    world.ledger.resolve(crashed.run_id, wl.UPPER_BOUND, 4_000_000)       # a resolution that does not waive
    still_set("an upper_bound resolution")
    world.fake.before_put(DEC, lambda: rewrite(world, DEC, add_settled("2026-12-12T1000-v1-manual")))
    silent = start(world, W3)                                             # a write that lost, then won
    still_set("a retried admission")
    world.clock.set(silent.expires_at)
    world.ledger.expire()
    still_set("an abandonment")
    world.ledger.resolve(silent.run_id, wl.UPPER_BOUND, 1)
    world.fake.script_put(DEC, fakes.LAND_THEN_500)
    start(world, W4)
    still_set("a write whose reply failed")
    assert world.ledger.assurance_waived("2026-12") is True


# ============================================= what an entry counts at ==

def test_a_resolved_entry_counts_at_its_settled_figure_in_the_next_admission(world):
    seed(world, "2026-12", [an_entry("2026-12-02T0400-pipeline", wl.UNRESOLVED, kind=wl.CRASHED)])
    world.ledger.resolve("2026-12-02T0400-pipeline", wl.UPPER_BOUND, CAP - R + 1)
    world.clock.set("2026-12-09T04:00:30Z")
    held = take(world, W2)
    stop = stopped(lambda: world.ledger.admit(held, R), wl.OVER_CAP)      # the bound errs towards blocking
    assert stop.counts["month_total_usd_micros"] == CAP - R + 1

    seed(world, "2026-12", [an_entry("2026-12-02T0400-pipeline", wl.UNRESOLVED, kind=wl.CRASHED)])
    world.ledger.resolve("2026-12-02T0400-pipeline", wl.UPPER_BOUND, CAP - R)
    assert world.ledger.admit(held, R)                                    # the partner: one micro less


# ================================================ the monthly bound, pinned ==

ONE_CALL = 159_918            # micro-dollars: the dearest single request, by the planning figures


def _month(world, second_basis: str, second_figure: int, second_true_cost: int) -> tuple:
    """December, four Wednesdays, with invented costs in micro-dollars. Run 2 is
    unmetered: it counted 2.0 million, and truly cost `second_true_cost`, because
    its input was not capped. The last run goes to one micro-dollar below its
    reservation and then makes one more call. Returns what was booked at each
    admission (B + R), the true spend, and the month's flag."""
    true_cost, booked = [], []

    def admit(when: str) -> wl.Admission:
        world.clock.set(store.parse_utc(when) + timedelta(seconds=30))
        held = take(world, when)
        total = wl.month_total(world.fake.json(DEC)) if DEC in world.fake.keys() else 0
        admitted = world.ledger.admit(held, R)
        booked.append(total + R)                                          # B + R, at this admission
        return admitted

    first = admit(W1)
    world.ledger.settle(first, report(first, total_nanos=1_900_000_000))
    true_cost.append(1_900_000)
    second = admit(W2)
    world.ledger.settle(second, report(second, total_nanos=2_000_000_000, unmetered=True))
    true_cost.append(second_true_cost)
    world.ledger.resolve(second.run_id, second_basis, second_figure)
    third = admit(W3)
    world.ledger.settle(third, report(third, total_nanos=3_550_000_000))
    true_cost.append(3_550_000)
    last = admit(W4)
    world.ledger.settle(last, report(last, total_nanos=(R - 1 + ONE_CALL) * 1000))
    true_cost.append(R - 1 + ONE_CALL)
    return booked, sum(true_cost), world.fake.json(DEC)["assurance_waived"]


def test_with_an_upper_bound_the_months_true_spend_passes_the_cap_by_at_most_one_call(world):
    """The proof in evals/ws7_ledger.py. What was booked at each admission never
    passed the cap, and each earlier entry counted at no less than it truly cost, so
    the true spend is within the cap plus the last run's final call."""
    booked, true_spend, waived = _month(world, wl.UPPER_BOUND, 5_000_000, second_true_cost=5_000_000)
    assert len(booked) == 4 and all(total <= CAP for total in booked)
    assert CAP < true_spend <= CAP + ONE_CALL          # past the cap, and by no more than one call
    assert waived is False
    world.clock.set("2026-12-30T04:00:30Z")            # and the month is now full
    held = take(world, W5)
    stopped(lambda: world.ledger.admit(held, R), wl.OVER_CAP)


def test_with_an_accepted_estimate_the_bound_can_fail_and_the_month_says_so(world):
    """The partner. Every admission still booked within the cap, but run 2 counted
    below its true cost, so the true spend passes the cap by far more than one call.
    That is the one way the bound fails, and it is exactly what the flag records."""
    booked, true_spend, waived = _month(world, wl.ESTIMATE_ACCEPTED, 2_000_000,
                                        second_true_cost=9_000_000)
    assert len(booked) == 4 and all(total <= CAP for total in booked)
    assert true_spend > CAP + ONE_CALL                 # the bound did not hold
    assert waived is True                              # and the month is marked


def test_what_was_booked_before_a_run_is_fixed_once_that_run_is_admitted(world):
    """Step (a) of the proof: no earlier entry is open or unresolved at an admission,
    and none can change afterwards."""
    first = start(world, W1)
    world.ledger.settle(first, report(first))
    second = start(world, W2)
    earlier = [e for e in world.fake.json(DEC)["entries"] if e["run_id"] != second.run_id]
    assert all(e["state"] in (wl.SETTLED, wl.RESOLVED) for e in earlier)
    world.ledger.settle(second, report(second, total_nanos=4_000_000_000, unmetered=True))
    world.ledger.resolve(second.run_id, wl.UPPER_BOUND, 5_000_000)
    assert [e for e in world.fake.json(DEC)["entries"] if e["run_id"] != second.run_id] == earlier


# ==================================================== messages and metrics ==

def test_no_stop_and_no_stored_object_carries_text(world):
    """Sentinel text is planted wherever text could enter: a guard report's extra
    fields, a scheduled time, a basis, and a ledger another writer left."""
    stops = []
    world.clock.set("2026-12-02T04:00:30Z")
    stops.append(stopped(lambda: take(world, f"{W1} {SENTINEL}")))
    admitted = start(world, W1)
    noisy = report(admitted, note=SENTINEL, calls=[{"step": "combined", "text": SENTINEL}])
    assert SENTINEL in json.dumps(noisy)                                  # the control: it was there
    stops.append(stopped(lambda: world.ledger.settle(admitted, dict(noisy, target=SENTINEL))))
    stops.append(stopped(lambda: world.ledger.settle(admitted, dict(
        noisy, batches={f"{admitted.run_id}.main": {}, SENTINEL: {}}))))
    world.ledger.settle(admitted, noisy)                                  # accepted: only numbers are read
    blocker = leave_crashed(world, W2)
    stops.append(stopped(lambda: world.ledger.resolve(blocker, SENTINEL, R)))
    world.clock.set("2026-12-16T04:00:30Z")
    held = take(world, W3)
    stops.append(stopped(lambda: world.ledger.admit(held, R)))            # blocked: names the run
    stops.append(stopped(lambda: take(world, W3)))                        # a duplicate

    written = {k: world.fake.body(k).decode("utf-8") for k in world.fake.keys()}
    assert len(written) == 4 and not any(SENTINEL in body for body in written.values())
    for stop in stops:
        assert SENTINEL not in stop.line() and SENTINEL not in str(stop)
        assert all(store.ID_RE.fullmatch(i) for i in stop.ids)
        assert all(type(v) is int for v in stop.counts.values())
    assert [s.code for s in stops] == [wl.SCHEDULED_TIME_INVALID, wl.REPORT_INVALID, wl.REPORT_INVALID,
                                       wl.BASIS_UNKNOWN, wl.BLOCKED_UNRESOLVED, wl.DUPLICATE]
    assert [s.metric for s in stops] == ["RunRefused"] * 5 + ["Duplicate"]

    bad = a_ledger("2026-12", [])
    bad["note"] = SENTINEL
    world.fake.seed(DEC, _raw(bad))
    assert SENTINEL not in stopped(world.ledger.snapshot, wl.LEDGER_INVALID).line()


def test_every_refusal_has_a_fixed_code():
    assert len(set(wl.CODES)) == len(wl.CODES) == 26
    assert all(code.startswith("ws7_") and store._LABEL_RE.fullmatch(code) for code in wl.CODES)
    assert not set(wl.CODES) & set(store.CODES) and not set(wl.CODES) & set(wc.CODES)


def test_reading_the_ledger_changes_nothing(world):
    blocker = leave_unmetered(world, W1)
    before, puts = ledger_bodies(world), len(world.fake.puts)
    assert world.ledger.snapshot()["2026-12"]["entries"][0]["run_id"] == blocker
    assert world.ledger.unresolved() == [("2026-12", blocker, wl.UNMETERED)]
    assert world.ledger.assurance_waived("2026-12") is False
    assert ledger_bodies(world) == before and len(world.fake.puts) == puts


# ====================================== Codex review of PR #6, rulings 1 to 4 ==

# ---------------------------- 1: the re-read after booking meets the rules again

LATE_NOV = "2026-11-30T2330-pipeline-manual"       # an invented manual run, booked in November


def _other_month_entry(what: str) -> dict:
    """An entry in November's ledger, as another run left it at the moment named."""
    if what == "unresolved":                        # booked, ran, and came back unmetered
        return an_entry(LATE_NOV, wl.UNRESOLVED, mode="manual", kind=wl.UNMETERED,
                        counted=2_000_000, estimate=2_000_000,
                        opened="2026-11-30T23:30:10Z", expires="2026-12-01T04:30:10Z")
    if what == "open_past_expiry":                  # booked, and died without a word
        return an_entry(LATE_NOV, wl.OPEN, mode="manual",
                        opened="2026-11-30T23:30:10Z", expires="2026-12-01T04:30:10Z")
    return an_entry(LATE_NOV, wl.OPEN, mode="manual",       # "live": booked, still running
                    opened="2026-12-02T04:00:29Z", expires="2026-12-02T09:00:29Z")


def test_a_manual_run_that_becomes_unresolved_between_the_scan_and_the_re_read_withdraws_the_scheduled_run(world):
    """Ruling 1 (Codex P1). The schedule, explicit and with no waiting:

    1. the scheduled run scans every month: November has no ledger;
    2. it books in December's ledger (its compare-and-swap lands);
    3. a manual run in NOVEMBER's ledger books and becomes unresolved;
    4. the scheduled run's final re-read finds it, and the run withdraws at zero.

    Step 3 is run by the fake at the instant step 2's write lands."""
    world.fake.after_put(DEC, lambda: seed(world, "2026-11", [_other_month_entry("unresolved")]))
    world.clock.set("2026-12-02T04:00:30Z")
    held = take(world, W1)
    stop = stopped(lambda: world.ledger.admit(held, R), wl.BLOCKED_UNRESOLVED)
    assert stop.ids == (LATE_NOV,) and stop.counts == {"unresolved": 1}

    # The schedule happened in that order: no read of November before December's write.
    log = world.fake.requests
    booked = log.index(("PUT", DEC))
    assert ("GET", NOV) not in log[:booked] and ("GET", NOV) in log[booked:]
    assert ledger_puts(world) == [(DEC, "if-none-match", 200), (DEC, "if-match", 200)]   # booked, withdrawn
    mine = entry_of(world, held.run_id)
    assert (mine["state"], mine["basis"], mine["counted_usd_micros"]) == (wl.SETTLED, wl.WITHDRAWN, 0)
    assert wl.month_total(world.fake.json(DEC)) == 0                      # nothing left reserved


def test_a_manual_run_that_becomes_unresolved_before_the_scheduled_runs_put_lands_withdraws_it(world):
    """Codex's exact timing (finding 1). The schedule, explicit and with no waiting:

    1. the scheduled run scans every month: November has no ledger;
    2. a manual run in NOVEMBER's ledger books and becomes unresolved;
    3. the scheduled run's PUT to December's ledger lands (November's object is not
       the one its compare-and-swap guards, so nothing stops it);
    4. the scheduled run's final re-read finds the entry, and the run withdraws at zero.

    Step 2 is run by the fake when the PUT of step 3 arrives, before it is judged."""
    world.fake.before_put(DEC, lambda: seed(world, "2026-11", [_other_month_entry("unresolved")]))
    world.clock.set("2026-12-02T04:00:30Z")
    held = take(world, W1)
    stop = stopped(lambda: world.ledger.admit(held, R), wl.BLOCKED_UNRESOLVED)
    assert stop.ids == (LATE_NOV,)

    log = world.fake.requests
    booked = log.index(("PUT", DEC))
    assert ("GET", NOV) not in log[:booked] and ("GET", NOV) in log[booked:]   # unseen by the scan
    assert ledger_puts(world) == [(DEC, "if-none-match", 200), (DEC, "if-match", 200)]   # booked, withdrawn
    mine = entry_of(world, held.run_id)
    assert (mine["state"], mine["basis"], mine["counted_usd_micros"]) == (wl.SETTLED, wl.WITHDRAWN, 0)


def test_with_no_such_entry_the_scheduled_run_is_admitted(world):
    """The partner: the same admission, and nothing appears in another month."""
    world.clock.set("2026-12-02T04:00:30Z")
    held = take(world, W1)
    admitted = world.ledger.admit(held, R)
    assert entry_of(world, admitted.run_id)["state"] == wl.OPEN
    assert ledger_puts(world) == [(DEC, "if-none-match", 200)]            # booked; no withdrawal


@pytest.mark.parametrize("mode,what,code", [
    ("scheduled", "unresolved", wl.BLOCKED_UNRESOLVED),
    ("scheduled", "open_past_expiry", wl.BLOCKED_UNRESOLVED),
    ("scheduled", "live", wl.RUN_IN_PROGRESS),
    ("manual", "live", wl.RUN_IN_PROGRESS),
    ("manual", "unresolved", None),                 # a manual run is not blocked by these
    ("manual", "open_past_expiry", None),
])
def test_the_re_read_applies_admissions_own_rules_leaving_this_run_out(world, mode, what, code):
    world.fake.after_put(DEC, lambda: seed(world, "2026-11", [_other_month_entry(what)]))
    world.clock.set("2026-12-02T04:00:30Z")
    held = take(world, W1, mode=mode)
    if code is None:
        assert world.ledger.admit(held, R).run_id == held.run_id          # its own open entry never blocks it
        assert entry_of(world, held.run_id)["state"] == wl.OPEN
    else:
        stop = stopped(lambda: world.ledger.admit(held, R), code)
        assert stop.ids == (LATE_NOV,)
        mine = entry_of(world, held.run_id)
        assert (mine["state"], mine["basis"], mine["counted_usd_micros"]) == (wl.SETTLED, wl.WITHDRAWN, 0)


# ------------------------------------- 2(a): the last try, when its reply failed

def test_the_last_trys_write_landed_though_its_reply_failed_and_the_run_starts(world):
    """Ruling 2(a). Every earlier try lost; the last one landed and its reply failed.
    There is no next try to read it back, so the ledger is read once more."""
    seed(world, "2026-12", [an_entry("2026-12-01T1000-v1-manual", mode="manual")])
    world.fake.script_put(DEC, *[fakes.CONFLICT_409] * (ATTEMPTS - 1), fakes.LAND_THEN_500)
    admitted = start(world, W1)
    assert len(ledger_puts(world)) == ATTEMPTS                            # no write beyond the count
    assert [e["run_id"] for e in world.fake.json(DEC)["entries"]].count(admitted.run_id) == 1
    assert entry_of(world, admitted.run_id)["state"] == wl.OPEN


def test_the_last_trys_write_did_not_land_and_the_run_does_not_start(world):
    seed(world, "2026-12", [an_entry("2026-12-01T1000-v1-manual", mode="manual")])
    world.fake.script_put(DEC, *[fakes.CONFLICT_409] * (ATTEMPTS - 1), fakes.FAIL_500)
    world.clock.set("2026-12-02T04:00:30Z")
    held = take(world, W1)
    before = world.fake.body(DEC)
    stop = stopped(lambda: world.ledger.admit(held, R), wl.LEDGER_CONFLICT)
    assert stop.counts == {"attempts": ATTEMPTS}
    assert len(ledger_puts(world)) == ATTEMPTS and world.fake.body(DEC) == before


def _on_the_last_put(world, action) -> None:
    """Run `action` just before, or just after, the last allowed PUT of December's
    ledger, and nothing around the earlier ones."""
    for _ in range(ATTEMPTS - 1):
        world.fake.before_put(DEC, lambda: None)
    world.fake.before_put(DEC, action)


def test_the_last_try_landed_and_another_writer_then_wrote_so_the_entry_is_credited(world):
    """The last allowed PUT lands and its reply fails; before the final read, another
    writer writes to the same month. The write_id on the ledger is no longer this
    call's, so the final read reconciles as an earlier retry would: the run's own
    entry is there, open, at its reservation, and nothing more is written."""
    seed(world, "2026-12", [an_entry("2026-12-01T1000-v1-manual", mode="manual")])
    world.fake.script_put(DEC, *[fakes.CONFLICT_409] * (ATTEMPTS - 1), fakes.LAND_THEN_500)
    world.fake.after_put(DEC, lambda: rewrite(world, DEC, add_settled("2026-12-01T1100-v1-manual")))
    admitted = start(world, W1)
    assert len(ledger_puts(world)) == ATTEMPTS                            # no write beyond the count
    runs = [e["run_id"] for e in world.fake.json(DEC)["entries"]]
    assert runs.count(admitted.run_id) == 1 and "2026-12-01T1100-v1-manual" in runs
    assert entry_of(world, admitted.run_id)["state"] == wl.OPEN
    assert admitted.expires_at == store.parse_utc(entry_of(world, admitted.run_id)["expires_at"])


def test_the_last_try_did_not_land_and_another_writer_wrote_so_it_is_a_conflict(world):
    """The partner. The last PUT does not land. Another writer has written, so there
    is no write_id to find, and the run's entry is not in the wanted state: it is
    not there at all. Reconciling would need a write, and the tries are spent."""
    seed(world, "2026-12", [an_entry("2026-12-01T1000-v1-manual", mode="manual")])
    world.fake.script_put(DEC, *[fakes.CONFLICT_409] * (ATTEMPTS - 1), fakes.FAIL_500)
    _on_the_last_put(world, lambda: rewrite(world, DEC, add_settled("2026-12-01T1100-v1-manual")))
    world.clock.set("2026-12-02T04:00:30Z")
    held = take(world, W1)
    stop = stopped(lambda: world.ledger.admit(held, R), wl.LEDGER_CONFLICT)
    assert stop.counts == {"attempts": ATTEMPTS}
    assert len(ledger_puts(world)) == ATTEMPTS
    assert held.run_id not in [e["run_id"] for e in world.fake.json(DEC)["entries"]]


def test_a_settle_whose_last_try_landed_before_another_writer_is_credited(world):
    """The same reconciliation for a settle: its entry is found settled at its figure."""
    admitted = start(world, W1)
    world.fake.script_put(DEC, *[fakes.CONFLICT_409] * (ATTEMPTS - 1), fakes.LAND_THEN_500)
    world.fake.after_put(DEC, lambda: rewrite(world, DEC, add_settled("2026-12-02T0500-v1-manual")))
    settled = world.ledger.settle(admitted, report(admitted))
    assert (settled["state"], settled["basis"], settled["counted_usd_micros"]) \
        == (wl.SETTLED, wl.METERED, 1_903_211)
    assert entry_of(world, admitted.run_id) == settled
    assert len(ledger_puts(world)) == 1 + ATTEMPTS                        # the booking, then the tries


# ------------------------- 2(b): a different competing transition is not credited

@pytest.mark.parametrize("unmetered", [False, True])
def test_an_entry_abandoned_by_another_reader_is_not_taken_for_this_runs_settle(world, unmetered):
    """This run's settle fails to land and its reply fails. Meanwhile another reader
    marks the entry abandoned. On the next read the entry is not open, and it is not
    in the state the settle wanted (an abandonment differs by kind), so nothing is
    credited: the settle is refused and the entry stays abandoned."""
    admitted = start(world, W1)

    def abandoned_by_another_reader():
        def change(doc):
            wl._abandon(doc["entries"][-1], store.parse_utc("2026-12-02T04:20:00Z"))
        rewrite(world, DEC, change)

    world.fake.before_put(DEC, abandoned_by_another_reader)
    world.fake.script_put(DEC, fakes.FAIL_500)
    world.clock.set("2026-12-02T04:20:00Z")
    stop = stopped(lambda: world.ledger.settle(admitted, report(admitted, unmetered=unmetered)),
                   wl.ENTRY_NOT_OPEN)
    assert stop.ids == (admitted.run_id,)
    entry = entry_of(world, admitted.run_id)
    assert (entry["state"], entry["kind"], entry["counted_usd_micros"]) == (wl.UNRESOLVED,
                                                                            wl.ABANDONED, R)


def test_the_same_failed_reply_with_no_competitor_is_settled_on_the_next_try(world):
    """The partner: the settle's first reply fails, nothing else happens, it settles."""
    admitted = start(world, W1)
    world.fake.script_put(DEC, fakes.FAIL_500)
    assert world.ledger.settle(admitted, report(admitted))["state"] == wl.SETTLED


# --------------------------------------------- 3: a report bound to its own run

def test_another_runs_report_with_an_equal_reservation_is_refused(world):
    """Ruling 3 (Codex P2). Two runs of one target reserve the same amount, so the
    target and the reservation cannot tell their reports apart. The batch labels do."""
    first = start(world, W1)
    first_report = report(first, total_nanos=1_000_000_000)
    world.ledger.settle(first, first_report)
    second = start(world, W2)
    assert second.reservation_usd_micros == first.reservation_usd_micros
    assert first_report["target"] == second.target                        # all that used to be checked

    before = world.fake.body(DEC)
    stop = stopped(lambda: world.ledger.settle(second, first_report), wl.REPORT_INVALID)
    assert stop.ids == (second.run_id,)
    assert world.fake.body(DEC) == before and entry_of(world, second.run_id)["state"] == wl.OPEN

    settled = world.ledger.settle(second, report(second, total_nanos=3_000_000_000))    # the partner
    assert settled["counted_usd_micros"] == 3_000_000


def test_settle_and_mark_crashed_take_the_admission_not_a_run_id(world):
    admitted = start(world, W1)
    for call in (lambda: world.ledger.settle(admitted.run_id, report(admitted)),
                 lambda: world.ledger.mark_crashed(admitted.run_id),
                 lambda: world.ledger.mark_crashed(None)):
        with pytest.raises(TypeError):
            call()
    # An Admission that is not the one admit() returned for this entry is refused too.
    for change in ({"reservation_usd_micros": R - 1}, {"target": "v1"},
                   {"expires_at": admitted.expires_at + timedelta(hours=1)}, {"month": "2026-11"}):
        forged = dataclasses.replace(admitted, **change)
        stopped(lambda: world.ledger.mark_crashed(forged), wl.ADMISSION_INVALID)
    assert entry_of(world, admitted.run_id)["state"] == wl.OPEN
    assert world.ledger.mark_crashed(admitted)["kind"] == wl.CRASHED      # the partner


# -------------------------------------------- 4: expiry while a call is in flight

@pytest.mark.parametrize("call", ["settle", "mark_crashed"])
def test_an_entry_that_expires_during_the_call_is_recorded_abandoned_and_the_call_refused(world, call):
    """Ruling 4 (Codex P2). The clock is one second short of expires_at when the call
    starts, and at expires_at by the time its write is decided. That write records
    the abandonment, and the call is refused."""
    admitted = start(world, W2)
    close = {"settle": lambda: world.ledger.settle(admitted, report(admitted)),
             "mark_crashed": lambda: world.ledger.mark_crashed(admitted)}[call]
    puts = len(ledger_puts(world))
    world.clock.script(admitted.expires_at - timedelta(seconds=1), admitted.expires_at)
    stop = stopped(close, wl.ENTRY_NOT_OPEN)
    assert stop.ids == (admitted.run_id,)
    entry = entry_of(world, admitted.run_id)
    assert (entry["state"], entry["kind"], entry["counted_usd_micros"]) == (wl.UNRESOLVED,
                                                                            wl.ABANDONED, R)
    assert entry["closed_at"] == store.utc_text(admitted.expires_at)
    assert len(ledger_puts(world)) == puts + 1                            # recorded by this call's own write


@pytest.mark.parametrize("call,state", [("settle", wl.SETTLED), ("mark_crashed", wl.UNRESOLVED)])
def test_an_entry_still_live_when_the_write_is_decided_is_closed_as_asked(world, call, state):
    """The partner: one second earlier at each reading, the call does what it was asked."""
    admitted = start(world, W2)
    close = {"settle": lambda: world.ledger.settle(admitted, report(admitted)),
             "mark_crashed": lambda: world.ledger.mark_crashed(admitted)}[call]
    world.clock.script(admitted.expires_at - timedelta(seconds=2),
                       admitted.expires_at - timedelta(seconds=1))
    entry = close()
    assert entry["state"] == state and entry["kind"] in (None, wl.CRASHED)


def _admit_with_the_clock_at(world, final_reading_after_opening: int):
    """Admit W1's run with the clock at 04:00:30 for the scan and the booking, and
    `final_reading_after_opening` seconds later at the final re-read."""
    opened = store.parse_utc("2026-12-02T04:00:30Z")
    world.clock.set(opened)
    held = take(world, W1)
    world.clock.script(opened, opened, opened + timedelta(seconds=final_reading_after_opening))
    return held, opened


def test_an_admission_that_ends_at_its_own_deadline_withdraws_and_is_refused(world):
    """Ruling 4. Booking took so long that, by the final re-read, the run's deadline
    has come: no call may be sent, so the run withdraws at zero."""
    to_deadline = CFG.wall_clock_limit_scheduled_s - wl.DEADLINE_MARGIN_S
    held, opened = _admit_with_the_clock_at(world, to_deadline)
    stop = stopped(lambda: world.ledger.admit(held, R), wl.ADMITTED_PAST_DEADLINE)
    assert stop.ids == (held.run_id,)
    mine = entry_of(world, held.run_id)
    assert (mine["state"], mine["basis"], mine["counted_usd_micros"]) == (wl.SETTLED, wl.WITHDRAWN, 0)
    assert mine["closed_at"] == store.utc_text(opened + timedelta(seconds=to_deadline))


def test_an_admission_that_ends_one_second_before_its_deadline_starts(world):
    """The partner."""
    to_deadline = CFG.wall_clock_limit_scheduled_s - wl.DEADLINE_MARGIN_S
    held, opened = _admit_with_the_clock_at(world, to_deadline - 1)
    admitted = world.ledger.admit(held, R)
    assert admitted.deadline_at == opened + timedelta(seconds=to_deadline)
    assert admitted.seconds_to_deadline(world.clock()) == 1
    assert entry_of(world, held.run_id)["state"] == wl.OPEN
