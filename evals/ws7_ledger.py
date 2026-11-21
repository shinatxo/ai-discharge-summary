#!/usr/bin/env python3
"""WS7 claim and monthly ledger: what stops a run from starting (ADR-011 D7).

Nothing here calls a model. Every S3 write is conditional (evals/ws7_store.py).

THE CLAIM. One S3 object per schedule occurrence, created with
`If-None-Match: *`. The key comes from the scheduled time the job is given as
an argument, never from the clock:

    scheduled   ws7/claims/<target>/<UTC date of the scheduled time>
    manual      ws7/claims/<target>/manual/<run_id>

A second delivery of the same occurrence finds the key taken and stops as a
DUPLICATE: it reads no ledger, reserves nothing and spends nothing. A manual
run has a key of its own, so it can never take a scheduled occurrence's claim;
what stops a manual and a scheduled run overlapping is the ledger's open entry.

THE LEDGER. One object a month, `ws7/ledger/<YYYY-MM>.json`: the UTC month of the
run's scheduled time. Every write is a compare-and-swap: the month's first
ledger is created with `If-None-Match: *`, and every later write carries
`If-Match` with the ETag that was read. On a conflict the job reads everything
again and decides again, `ledger_cas_attempts` tries in total, then does not
start.

An entry is one run:

    open --------> settled      (metered: the guard's total; withdrawn: 0, no call made)
      |
      +----------> unresolved   (unmetered | abandoned | crashed)
                       |
                       +------> resolved   (upper_bound | estimate_accepted)

- `settled` and `resolved` never change again. Every write is checked against
  the ledger it replaces (`_check_transition`), so a write that would change
  one, drop an entry or clear the month's flag is refused before it is sent.
- What an entry counts at: open, its reservation; settled, the guard's counted
  total; unresolved-unmetered, the guard's counted total; unresolved-abandoned
  or -crashed, its reservation; resolved, the settled figure.

ADMISSION, in this order, decided again on every try:

1. An open entry in any month that has not reached its `expires_at` refuses
   the run: one run at a time (`ws7_run_in_progress`).
2. An open entry at or past `expires_at` is abandoned: it becomes unresolved.
3. A scheduled run is refused while any entry in any month is unresolved
   (`ws7_blocked_unresolved`). A manual run is not.
4. A scheduled run is admitted only if the month's recorded total plus its
   reservation is at most the cap (`ws7_over_cap`). Equal is admitted. A manual
   run is outside the cap: it is neither counted nor checked.
5. The open entry is written.

After its entry is written the run reads every month once more and meets rules
1 to 3 again, leaving itself out: an entry in another month's object, written
after this run's scan, is something no single compare-and-swap could see.

- A scheduled run withdraws if any other entry, in any month, is live, is
  open past its `expires_at`, or is unresolved.
- A manual run withdraws only for a live entry.
- Either withdraws if the clock has reached its own `deadline_at`: its
  admission took so long that no call may be sent.

It withdraws its own entry at zero (`settled`, `withdrawn`) and does not start.
Two runs that race across two objects each write, then each read: the later
read always sees the earlier write, so at least one withdraws.

HOW EACH UNRESOLVED CASE IS RECOGNISED

- unmetered: by the run itself. `settle()` is given a guard report that says
  `unmetered`, and writes the entry as unresolved in the same write.
- crashed: by the run itself. The entrypoint's outermost handler calls
  `mark_crashed()` when it has no guard report it can settle from.
- abandoned: by whoever looks next. A run that died outright reports nothing;
  its entry stays open, and the next call here (any mode, any target) finds it
  at or past `expires_at` and marks it. `expires_at` is stored when the entry
  is opened, so a later change to the config cannot move it.
- The overlap: a run that died before it could settle or report IS the abandoned
  case. Only an open entry can become unresolved, so an entry takes exactly
  one kind, the first one written. The kind is a label: all three block the
  same way and are resolved the same way.
- At or past `expires_at` the entry is abandoned whoever asks, the run itself
  included: a late `settle()` or `mark_crashed()` is refused
  (`ws7_entry_not_open`), and only `resolve()` moves the entry on. The clock is
  read again inside the write: if the entry expired while the call was reading
  S3, that write records the abandonment, and the call is then refused.

WHOSE REPORT. `settle()` and `mark_crashed()` take the Admission that `admit()`
returned, not a run ID, and the entry must be the one that Admission describes.
`settle()` also refuses a report that is not this run's own. The report's
`run_id` must be the Admission's: the guard is given the run's ID when it is
made and puts it in its report (evals/ws7_client.py), so a report with none,
or with another run's, is refused whatever else it holds, a fresh one with no
batch and no cost included. Its target and its reservation must be the
Admission's too, and every batch label in it must start with `<run_id>.`. The
entrypoint labels its batches `<run_id>.preflight` and `<run_id>.main` (part 5).

THE RUN'S OWN DEADLINE. `Admission.deadline_at` is `expires_at` less
DEADLINE_MARGIN_S. The entrypoint must give the client wrapper's guard that
deadline (part 5), so the last call is sent at least that long before another
task may mark the entry abandoned. The margin is the longest connect timeout
plus read timeout of either target. A read timeout bounds each wait for data,
not a whole reply, so this is a margin, not a proof that no call is in flight.

THE MONTHLY BOUND, restated from what was booked (Codex P3, PR #5)

Take one month, cap C. Its inside-cap (scheduled) runs are admitted one after
another, because every admission is a compare-and-swap on the month's one
object. Call the last one admitted r, with reservation R.

(a) What was booked at r's admission. Rule 4 held inside the write that booked
    r: B + R <= C, where B is the sum of what every earlier inside-cap entry
    counted at, at that moment. By rules 1 to 3 none of those entries was open
    or unresolved then, so each was settled or resolved, and by
    `_check_transition` none of them can change afterwards. B is therefore
    fixed at r's admission; nothing that happens later (r being settled, an
    entry in another month being resolved) alters it.
(b) Each term of B is at least that run's true cost, PROVIDED the entry is
    `metered` or `upper_bound`. A metered total prices every reply at the usage
    Bedrock returned and counts every call that did not run the model at its
    worst case (evals/ws7_client.py); an `upper_bound` figure is the author's
    statement that it is one. An `estimate_accepted` figure may be below the
    true cost, and a `withdrawn` entry made no call.
(c) r's own true cost is below R plus one call: the guard sends a call only
    while its running total is below R, and that total is a true cost up to
    the final call (evals/ws7_client.py, "How far a run can pass its
    reservation").

So the month's true inside-cap spend is at most B + R + r's final call, which
is at most C plus that one call. The proof uses three things only: what was
booked at each earlier run's admission into B, the last run's reservation, and
its final call. It does not use the month's recorded total as it stands later,
which a resolution can move either way.

The one way (b) fails is an `estimate_accepted` resolution, and that is exactly
what sets the month's `assurance_waived` flag. The flag is set in the same
write as the resolution and is never cleared. The loader refuses a month whose
flag and entries disagree, either way: an accepted estimate with no flag, or a
flag with no accepted estimate.

"True cost" is priced at the repository's rates (ADR-011, Costs). A month is
the UTC month of a run's scheduled time, not AWS's invoice month for each call.
"""

from __future__ import annotations

import json
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

_EVALS = Path(__file__).resolve().parent
if str(_EVALS) not in sys.path:
    sys.path.insert(0, str(_EVALS))

import ws7_reserve as reserve  # noqa: E402
import ws7_store as store  # noqa: E402
from ws7_store import MODES, SCHEDULED, TARGETS, Stop  # noqa: E402

CLAIM_SCHEMA = "ws7-claim/1"
LEDGER_SCHEMA = "ws7-ledger/1"

OPEN, SETTLED, UNRESOLVED, RESOLVED = "open", "settled", "unresolved", "resolved"
STATES = (OPEN, SETTLED, UNRESOLVED, RESOLVED)
UNMETERED, ABANDONED, CRASHED = "unmetered", "abandoned", "crashed"
KINDS = (UNMETERED, ABANDONED, CRASHED)
METERED, WITHDRAWN = "metered", "withdrawn"
UPPER_BOUND, ESTIMATE_ACCEPTED = "upper_bound", "estimate_accepted"
RESOLUTION_BASES = (UPPER_BOUND, ESTIMATE_ACCEPTED)

# The longest one call can wait on its connection and then on a read: v1's 60 s to
# connect plus 60 s to read. tests/test_ws7_ledger.py checks it against both plans.
DEADLINE_MARGIN_S = 120

DUPLICATE = "ws7_duplicate"
TARGET_UNKNOWN = "ws7_target_unknown"
MODE_UNKNOWN = "ws7_mode_unknown"
SCHEDULED_TIME_INVALID = "ws7_scheduled_time_invalid"
SCHEDULED_TIME_FUTURE = "ws7_scheduled_time_future"
START_TOO_LATE = "ws7_start_too_late"
CLAIM_UNAVAILABLE = "ws7_claim_unavailable"
CLAIM_INVALID = "ws7_claim_invalid"
RESERVATION_INVALID = "ws7_reservation_invalid"
WALL_CLOCK_LIMIT_TOO_SHORT = "ws7_wall_clock_limit_too_short"
RUN_IN_PROGRESS = "ws7_run_in_progress"
ADMITTED_PAST_DEADLINE = "ws7_admitted_past_deadline"
ADMISSION_INVALID = "ws7_admission_invalid"
BLOCKED_UNRESOLVED = "ws7_blocked_unresolved"
OVER_CAP = "ws7_over_cap"
LEDGER_CONFLICT = "ws7_ledger_conflict"
LEDGER_UNAVAILABLE = "ws7_ledger_unavailable"
LEDGER_INVALID = "ws7_ledger_invalid"
LEDGER_WRITE_INVALID = "ws7_ledger_write_invalid"
ENTRY_EXISTS = "ws7_entry_exists"
ENTRY_UNKNOWN = "ws7_entry_unknown"
ENTRY_NOT_OPEN = "ws7_entry_not_open"
ENTRY_NOT_UNRESOLVED = "ws7_entry_not_unresolved"
REPORT_INVALID = "ws7_report_invalid"
BASIS_UNKNOWN = "ws7_basis_unknown"
SETTLED_FIGURE_INVALID = "ws7_settled_figure_invalid"
ESTIMATE_DIFFERS = "ws7_estimate_differs"
CODES = (DUPLICATE, TARGET_UNKNOWN, MODE_UNKNOWN, SCHEDULED_TIME_INVALID, SCHEDULED_TIME_FUTURE,
         START_TOO_LATE, CLAIM_UNAVAILABLE, CLAIM_INVALID, RESERVATION_INVALID,
         WALL_CLOCK_LIMIT_TOO_SHORT,
         RUN_IN_PROGRESS, ADMITTED_PAST_DEADLINE, ADMISSION_INVALID, BLOCKED_UNRESOLVED, OVER_CAP, LEDGER_CONFLICT, LEDGER_UNAVAILABLE,
         LEDGER_INVALID, LEDGER_WRITE_INVALID, ENTRY_EXISTS, ENTRY_UNKNOWN, ENTRY_NOT_OPEN,
         ENTRY_NOT_UNRESOLVED, REPORT_INVALID, BASIS_UNKNOWN, SETTLED_FIGURE_INVALID,
         ESTIMATE_DIFFERS)

DUPLICATE_METRIC = "Duplicate"

_SCHEDULED_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})")
_TOKEN_RE = re.compile(r"[0-9a-f]{32}")


class Duplicate(Stop):
    """The occurrence is already claimed. Not a failure: the job exits, having
    reserved and spent nothing, and counts one duplicate (no alarm, ADR-011 D7)."""

    metric = DUPLICATE_METRIC


def _token() -> str:
    return secrets.token_hex(16)


def _dump(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


# ------------------------------------------------------------------- claim

@dataclass(frozen=True)
class Claim:
    """An occurrence this job holds."""
    target: str
    mode: str
    scheduled_time: str       # UTC, YYYY-MM-DDTHH:MM:SSZ
    run_id: str
    claim_id: str
    claimed_at: str
    key: str

    @property
    def month(self) -> str:
        """The ledger month: the UTC month of the scheduled time."""
        return self.scheduled_time[:7]


CLAIM_FIELDS = ("schema", "target", "mode", "scheduled_time", "run_id", "claim_id", "claimed_at")


def claim_fault(claim: object, *, key: str, target: str, mode: str,
                max_start_delay_s: int) -> str | None:
    """Why `claim` is not the claim object of `target` and `mode` that belongs at `key`:
    a fixed label naming the first field that fails, or None when it is that claim.

    ONE VALIDATOR, SHARED BY THE WRITER AND THE READER. `claim()` calls it on the
    object it is about to PUT, and the Thursday check (evals/ws7_entry.py) calls it on
    the object it has just read, so the job can never write a claim its own check
    would reject. EVERY FIELD IS CHECKED: its type, its pattern or range, and its
    agreement with the occurrence (the key, the target, the mode). An unknown or
    missing field is refused.

    - the object: a JSON object with exactly the seven fields (`fields` otherwise);
    - `schema`: the string `ws7-claim/1`;
    - `target`, `mode`: the strings this occurrence has;
    - `scheduled_time`: a UTC time, `YYYY-MM-DDTHH:MM:SSZ`, whose claim key is `key`;
    - `run_id`: the ID that target, mode and scheduled time give;
    - `claim_id`: 32 hex characters;
    - `claimed_at`: a UTC time, from the scheduled time to `max_start_delay_s` after it."""
    if type(claim) is not dict:
        return "not_an_object"
    if set(claim) != set(CLAIM_FIELDS):
        return "fields"
    if type(claim["schema"]) is not str or claim["schema"] != CLAIM_SCHEMA:
        return "schema"
    if type(claim["target"]) is not str or claim["target"] != target or target not in TARGETS:
        return "target"
    if type(claim["mode"]) is not str or claim["mode"] != mode or mode not in MODES:
        return "mode"
    when = store.parse_utc(claim["scheduled_time"])
    if when is None or store.claim_key(target, mode, when) != key:
        return "scheduled_time"                     # not a time, or not this occurrence's
    if type(claim["run_id"]) is not str or claim["run_id"] != store.run_id(target, mode, when):
        return "run_id"
    if type(claim["claim_id"]) is not str or not _TOKEN_RE.fullmatch(claim["claim_id"]):
        return "claim_id"
    claimed = store.parse_utc(claim["claimed_at"])
    if claimed is None or not 0 <= (claimed - when).total_seconds() <= max_start_delay_s:
        return "claimed_at"
    return None


def parse_scheduled_time(text: object) -> datetime:
    """The schedule's own scheduled time: ISO 8601 to the second, with `Z` or an
    explicit offset. A time with no zone is refused, not guessed."""
    if type(text) is not str or not _SCHEDULED_RE.fullmatch(text):
        raise Stop(SCHEDULED_TIME_INVALID)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise Stop(SCHEDULED_TIME_INVALID) from None
    return moment.astimezone(timezone.utc)


def claim(st: store.Store, cfg: reserve.Config, *, target: str, mode: str, scheduled_time: str,
          clock=store.utc_now, token=_token) -> Claim:
    """Claim one occurrence, or stop.

    Before any write: the scheduled time must not be in the future, and must be
    no older than `max_start_delay_s` (scheduled and manual alike). Then one
    object is created with `If-None-Match: *`. If the key is taken the job
    raises Duplicate. A write whose reply failed is read back: the claim is
    this job's only if the object carries this job's `claim_id`.

    The object is checked by `claim_fault` before it is sent (`ws7_claim_invalid`)."""
    if target not in TARGETS:
        raise Stop(TARGET_UNKNOWN)
    if mode not in MODES:
        raise Stop(MODE_UNKNOWN)
    scheduled = parse_scheduled_time(scheduled_time)
    now = store.read_clock(clock)
    if scheduled > now:
        raise Stop(SCHEDULED_TIME_FUTURE)
    if (now - scheduled).total_seconds() > cfg.max_start_delay_s:
        raise Stop(START_TOO_LATE)

    claim_id = token()
    if type(claim_id) is not str or not _TOKEN_RE.fullmatch(claim_id):
        raise ValueError("a claim ID is 32 hex characters")
    held = Claim(target, mode, store.utc_text(scheduled), store.run_id(target, mode, scheduled),
                 claim_id, store.utc_text(now), store.claim_key(target, mode, scheduled))
    written = {"schema": CLAIM_SCHEMA, "target": held.target, "mode": held.mode,
               "scheduled_time": held.scheduled_time, "run_id": held.run_id,
               "claim_id": held.claim_id, "claimed_at": held.claimed_at}
    # Before the PUT: the validator the Thursday check runs on what it reads. A claim
    # the check would reject is never written.
    fault = claim_fault(written, key=held.key, target=target, mode=mode,
                        max_start_delay_s=cfg.max_start_delay_s)
    if fault is not None:
        raise Stop(CLAIM_INVALID, ids=(fault,))
    try:
        outcome = st.create(held.key, _dump(written), attempts=cfg.ledger_cas_attempts)
    except store.StoreError as exc:
        raise Stop(CLAIM_UNAVAILABLE, ids=(held.run_id, exc.code)) from None
    if outcome != store.CREATED:
        raise Duplicate(DUPLICATE, ids=(held.run_id,), counts={"duplicates": 1})
    return held


# ------------------------------------------------------- the ledger's shape

_LEDGER_FIELDS = ("schema", "month", "rev", "write_id", "cap_usd_micros", "assurance_waived",
                  "entries")
_ENTRY_FIELDS = ("run_id", "target", "mode", "state", "kind", "basis", "reserved_usd_micros",
                 "counted_usd_micros", "estimate_usd_micros", "opened_at", "expires_at",
                 "closed_at", "resolved_at")
_FIXED_ENTRY_FIELDS = ("run_id", "target", "mode", "reserved_usd_micros", "opened_at", "expires_at")
_NEXT_STATES = {OPEN: (OPEN, SETTLED, UNRESOLVED), UNRESOLVED: (UNRESOLVED, RESOLVED),
                SETTLED: (SETTLED,), RESOLVED: (RESOLVED,)}


def _count(value: object) -> bool:
    return type(value) is int and value >= 0        # a bool is not a count


def _no_duplicate_keys(pairs: list) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _no_constants(_name: str):
    raise ValueError("NaN or Infinity")


def _entry_ok(e: object) -> bool:
    if type(e) is not dict or set(e) != set(_ENTRY_FIELDS):
        return False
    if not all(type(e[k]) is str for k in ("run_id", "target", "mode", "state")):
        return False
    if not store.ID_RE.fullmatch(e["run_id"]) or e["target"] not in TARGETS \
            or e["mode"] not in MODES or e["state"] not in STATES:
        return False
    reserved, counted, estimate = (e["reserved_usd_micros"], e["counted_usd_micros"],
                                   e["estimate_usd_micros"])
    if not _count(reserved) or reserved == 0 or not _count(counted):
        return False
    opened, expires = store.parse_utc(e["opened_at"]), store.parse_utc(e["expires_at"])
    if opened is None or expires is None or expires <= opened:
        return False
    for name in ("closed_at", "resolved_at"):
        if e[name] is not None and store.parse_utc(e[name]) is None:
            return False
    kind, basis = e["kind"], e["basis"]
    closed, resolved = e["closed_at"] is not None, e["resolved_at"] is not None
    worst_is_reservation = kind in (ABANDONED, CRASHED)
    if e["state"] == OPEN:
        return (kind is None and basis is None and estimate is None and counted == reserved
                and not closed and not resolved)
    if e["state"] == SETTLED:
        return (kind is None and basis in (METERED, WITHDRAWN) and estimate is None and closed
                and not resolved and (basis != WITHDRAWN or counted == 0))
    if kind not in KINDS or not _count(estimate) or not closed \
            or (worst_is_reservation and estimate != reserved):
        return False
    if e["state"] == UNRESOLVED:
        return basis is None and counted == estimate and not resolved
    return (basis in RESOLUTION_BASES and resolved
            and (basis != ESTIMATE_ACCEPTED or counted == estimate))


def parse_ledger(raw: bytes, month: str) -> dict:
    """One month's ledger, strictly: an unknown or missing field, a wrong type, a
    duplicate key or a state that cannot be is an error, never repaired."""
    bad = Stop(LEDGER_INVALID, ids=(month,))
    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys,
                         parse_constant=_no_constants)
    except (ValueError, RecursionError):
        raise bad from None
    if type(doc) is not dict or set(doc) != set(_LEDGER_FIELDS) \
            or doc["schema"] != LEDGER_SCHEMA or doc["month"] != month:
        raise bad
    if not _count(doc["rev"]) or doc["rev"] < 1 \
            or type(doc["write_id"]) is not str or not _TOKEN_RE.fullmatch(doc["write_id"]) \
            or not _count(doc["cap_usd_micros"]) or doc["cap_usd_micros"] == 0 \
            or type(doc["assurance_waived"]) is not bool or type(doc["entries"]) is not list:
        raise bad
    entries = doc["entries"]
    if not all(_entry_ok(e) for e in entries) \
            or len({e["run_id"] for e in entries}) != len(entries):
        raise bad
    # The flag and its cause travel together, in both directions. An accepted estimate
    # with no flag is a ledger someone has altered; so is a flag with no accepted
    # estimate, which nothing here writes (only `resolve()` sets the flag, in the same
    # write as the entry that causes it, and a resolved entry is never dropped).
    if doc["assurance_waived"] != any(e["basis"] == ESTIMATE_ACCEPTED for e in entries):
        raise bad
    return doc


def _check_transition(old: dict | None, new: dict) -> None:
    """Refuse a write that the rules do not allow, before it is sent.

    The flag is never cleared. No entry is dropped or reordered. A settled or
    resolved entry never changes. An entry moves only open -> settled, open ->
    unresolved or unresolved -> resolved. A new entry is appended, open."""
    bad = Stop(LEDGER_WRITE_INVALID, ids=(new["month"],))
    before = old["entries"] if old else []
    after = new["entries"]
    if new["rev"] != (old["rev"] + 1 if old else 1):
        raise bad
    if old and old["assurance_waived"] and not new["assurance_waived"]:
        raise bad
    if len(after) < len(before):
        raise bad
    for was, now in zip(before, after):
        if any(was[f] != now[f] for f in _FIXED_ENTRY_FIELDS) \
                or now["state"] not in _NEXT_STATES[was["state"]]:
            raise bad
        if now["state"] == was["state"] and now != was:
            raise bad
    if any(e["state"] != OPEN for e in after[len(before):]):
        raise bad


def _entries(snap: dict):
    for month in sorted(snap):
        for entry in snap[month][0]["entries"]:
            yield month, entry


def _find(snap: dict, run: str):
    return next(((m, e) for m, e in _entries(snap) if e["run_id"] == run), None)


def _is_live(entry: dict, now: datetime) -> bool:
    """Open, and not yet at its `expires_at`."""
    return entry["state"] == OPEN and now < store.parse_utc(entry["expires_at"])


def _abandon(entry: dict, now: datetime) -> None:
    entry.update(state=UNRESOLVED, kind=ABANDONED, estimate_usd_micros=entry["reserved_usd_micros"],
                 counted_usd_micros=entry["reserved_usd_micros"], closed_at=store.utc_text(now))


def month_total(doc: dict) -> int:
    """The month's recorded total: what its inside-cap (scheduled) entries count at."""
    return sum(e["counted_usd_micros"] for e in doc["entries"] if e["mode"] == SCHEDULED)


# --------------------------------------------------------------- admission

@dataclass(frozen=True)
class Admission:
    """A run that may start. `deadline_at` is the guard's deadline: `expires_at`
    less DEADLINE_MARGIN_S (see the module docstring)."""
    run_id: str
    target: str
    mode: str
    month: str
    reservation_usd_micros: int
    opened_at: datetime
    expires_at: datetime
    deadline_at: datetime

    def seconds_to_deadline(self, now: datetime) -> float:
        return (self.deadline_at - now).total_seconds()


def _admission(month: str, entry: dict) -> Admission:
    expires = store.parse_utc(entry["expires_at"])
    return Admission(entry["run_id"], entry["target"], entry["mode"], month,
                     entry["reserved_usd_micros"], store.parse_utc(entry["opened_at"]), expires,
                     expires - timedelta(seconds=DEADLINE_MARGIN_S))


def _read_report(report: object, admitted: Admission) -> tuple:
    """What `settle` needs from the guard's report (evals/ws7_client.py): the counted
    total in micro-dollars, and whether the run is unmetered. The report must be
    this run's own: the Admission's run ID, target and reservation, and batch labels
    that all start with `<run_id>.`."""
    bad = Stop(REPORT_INVALID, ids=(admitted.run_id,))
    if type(report) is not dict:
        raise bad
    target, unmetered = report.get("target"), report.get("unmetered")
    total, micros = report.get("total_usd_nanos"), report.get("total_usd_micros")
    reservation, batches = report.get("reservation_usd_nanos"), report.get("batches")
    if type(target) is not str or target not in TARGETS or type(unmetered) is not bool \
            or not _count(total) or not _count(reservation) or reservation == 0 \
            or micros != reserve.to_micros(total) or type(micros) is not int:
        raise bad
    whose = report.get("run_id")
    if type(whose) is not str or whose != admitted.run_id:
        raise bad                                   # no run ID, or another run's
    if target != admitted.target \
            or reserve.to_micros(reservation) != admitted.reservation_usd_micros:
        raise bad
    mine = admitted.run_id + "."
    if type(batches) is not dict or not all(
            type(label) is str and label.startswith(mine) and len(label) > len(mine)
            for label in batches):
        raise bad
    return micros, unmetered


def _blocked(snap: dict, now: datetime, mode: str, *, exclude: str | None = None) -> Stop | None:
    """Admission rules 1 to 3, over every month's entries but the run's own: the stop
    a run meets, or None. Used when a run is booked and again after it is.

    Every run: any live open entry. A scheduled run as well: any unresolved entry,
    and any entry open past its `expires_at`, which is abandoned in all but the
    write that says so."""
    others = [e for _, e in _entries(snap) if e["run_id"] != exclude]
    live = tuple(e["run_id"] for e in others if _is_live(e, now))
    if live:
        return Stop(RUN_IN_PROGRESS, ids=live)
    if mode == SCHEDULED:
        blockers = tuple(e["run_id"] for e in others if e["state"] in (OPEN, UNRESOLVED))
        if blockers:
            return Stop(BLOCKED_UNRESOLVED, ids=blockers, counts={"unresolved": len(blockers)})
    return None


class Ledger:
    """The monthly ledgers, through one Store. `clock` gives the wall-clock time in
    UTC; it is compared with times other tasks wrote, so it is not a monotonic clock."""

    def __init__(self, st: store.Store, cfg: reserve.Config, *, clock=store.utc_now, token=_token):
        self._store = st
        self._cfg = cfg
        self._clock = clock
        self._token = token

    # ----------------------------------------------------------- reading

    def _scan(self) -> dict:
        """Every month's ledger: month -> (ledger, ETag). The prefix is listed first
        and only listed keys are read, so a month with no ledger is known from
        the listing, never from a GET error."""
        try:
            snap = {}
            for key in self._store.list_keys(store.LEDGER_PREFIX):
                month = store.ledger_month(key)
                if month is None:
                    raise Stop(LEDGER_INVALID)          # something else is under the prefix
                raw, etag = self._store.get(key)
                snap[month] = (parse_ledger(raw, month), etag)
        except store.StoreError as exc:
            raise Stop(LEDGER_UNAVAILABLE, ids=(exc.code,)) from None
        runs = [e["run_id"] for _, e in _entries(snap)]
        if len(set(runs)) != len(runs):
            raise Stop(LEDGER_INVALID)                  # one run in two months
        return snap

    def snapshot(self) -> dict:
        """Every month's ledger as stored: month -> ledger. Read-only."""
        return {month: doc for month, (doc, _etag) in self._scan().items()}

    def unresolved(self) -> list:
        """(month, run_id, kind) for every unresolved entry, oldest month first."""
        return [(m, e["run_id"], e["kind"]) for m, e in _entries(self._scan())
                if e["state"] == UNRESOLVED]

    def assurance_waived(self, month: str) -> bool:
        """Whether `month` is marked "assurance waived". A month with no ledger is not."""
        snap = self._scan()
        return month in snap and snap[month][0]["assurance_waived"]

    # ----------------------------------------------------------- writing

    def _cas(self, decide):
        """One change to one month's ledger, by compare-and-swap.

        `decide(snap, now, unsure)` is called afresh on every try with everything
        just read. It returns `(month, entries, waive, result)`, or a `month` of
        None when there is nothing to write; it raises Stop to refuse. A `result`
        that is a Stop is raised once its write has landed: the write records
        something, and the call is still refused.

        `ledger_cas_attempts` counts tries IN TOTAL. A try that lost (412, or a
        409) is followed by a new read and a new decision.

        A try whose reply failed may have landed. Its `write_id` is remembered,
        and if a later read shows it, that write is this call's and its result
        stands. After the LAST try, if any reply failed, the ledger is read once
        more, and nothing more is written: the write is there, or the call ends
        with ws7_ledger_conflict.

        If another writer has written since, the `write_id` is no longer on the
        ledger, and `decide` is told `unsure`. It then treats an entry that is
        already in exactly the state it wanted as done. That needs no operation
        ID, because no other writer can make the same change to this entry:

        - booking, settling, withdrawing and the crash mark are made only by the
          run's own process, which holds the run's claim, and the claim is
          exclusive;
        - the one change another reader makes to someone else's entry is
          abandonment, which differs by kind (`abandoned`), so it is never taken
          for this call's settle, withdrawal or crash mark;
        - a resolution is made only by the author's command.

        So an entry found in exactly the wanted state was put there by an earlier
        try of this call, and the end state is the one intended either way. A
        DIFFERENT state is not credited: the call is refused."""
        def finish(result):
            if isinstance(result, Stop):
                raise result
            return result

        pending: dict = {}
        for _ in range(self._cfg.ledger_cas_attempts):
            snap = self._scan()
            for doc, _etag in snap.values():
                if doc["write_id"] in pending:
                    return finish(pending[doc["write_id"]])
            now = store.read_clock(self._clock)
            month, entries, waive, result = decide(snap, now, bool(pending))
            if month is None:
                return result
            old, etag = snap.get(month, (None, None))
            new = {"schema": LEDGER_SCHEMA, "month": month, "rev": old["rev"] + 1 if old else 1,
                   "write_id": self._token(), "cap_usd_micros": self._cfg.cap_usd_micros,
                   # Once set, the flag is carried by every later write.
                   "assurance_waived": bool(old and old["assurance_waived"]) or waive,
                   "entries": entries}
            body = _dump(new)
            parse_ledger(body, month)                   # never write what would not be read back
            _check_transition(old, new)
            try:
                if old is None:
                    outcome, _ = self._store.put_if_absent(store.ledger_key(month), body)
                else:
                    outcome, _ = self._store.put_if_match(store.ledger_key(month), body, etag)
            except store.StoreError as exc:
                raise Stop(LEDGER_UNAVAILABLE, ids=(exc.code,)) from None
            if outcome == store.WRITTEN:
                return finish(result)
            if outcome == store.AMBIGUOUS:
                pending[new["write_id"]] = result
        if pending:
            # The last try's reply failed, so it was never read back. Read once more and
            # reconcile exactly as an earlier retry would, but WRITE NOTHING: first the
            # write_id, then the decision with `unsure`. If the decision has nothing to
            # write (the entry is already in exactly the wanted state), that is this
            # call's own write and its result stands. If it would write, the tries are
            # spent: ws7_ledger_conflict. A refusal it raises is raised.
            last = self._scan()
            for doc, _etag in last.values():
                if doc["write_id"] in pending:
                    return finish(pending[doc["write_id"]])
            would_write, _entries_, _waive, reconciled = decide(
                last, store.read_clock(self._clock), True)
            if would_write is None:
                return reconciled
        raise Stop(LEDGER_CONFLICT, counts={"attempts": self._cfg.ledger_cas_attempts})

    def expire(self) -> None:
        """Mark every open entry at or past its `expires_at` as abandoned. Every
        call that changes the ledger does this first; the Thursday check may call
        it too (part 5)."""
        now = store.read_clock(self._clock)
        for _month, entry in list(_entries(self._scan())):
            if entry["state"] == OPEN and not _is_live(entry, now):
                self._cas(self._decide_abandon(entry["run_id"]))

    @staticmethod
    def _decide_abandon(run: str):
        def decide(snap: dict, now: datetime, _unsure: bool) -> tuple:
            found = _find(snap, run)
            if found is None or found[1]["state"] != OPEN or _is_live(found[1], now):
                return None, None, False, None          # someone else has dealt with it
            month = found[0]
            entries = [dict(e) for e in snap[month][0]["entries"]]
            _abandon(next(e for e in entries if e["run_id"] == run), now)
            return month, entries, False, None
        return decide

    def _limit_s(self, mode: str) -> int:
        limit = (self._cfg.wall_clock_limit_scheduled_s if mode == SCHEDULED
                 else self._cfg.wall_clock_limit_manual_s)
        if limit <= DEADLINE_MARGIN_S:
            raise Stop(WALL_CLOCK_LIMIT_TOO_SHORT)
        return limit

    def admit(self, held: Claim, reservation_usd_micros: int) -> Admission:
        """Book the run's reservation, or stop. See ADMISSION in the module
        docstring. Nothing is spent before this returns."""
        if not isinstance(held, Claim):
            raise TypeError("admit() needs the Claim this job holds")
        if not _count(reservation_usd_micros) or reservation_usd_micros == 0:
            raise Stop(RESERVATION_INVALID)
        limit = self._limit_s(held.mode)
        run, month, cap = held.run_id, held.month, self._cfg.cap_usd_micros

        def book(snap: dict, now: datetime, unsure: bool) -> tuple:
            mine = _find(snap, run)
            if mine is not None:
                if unsure and mine[0] == month and mine[1]["state"] == OPEN \
                        and mine[1]["reserved_usd_micros"] == reservation_usd_micros:
                    return None, None, False, _admission(month, mine[1])    # an earlier try landed
                raise Stop(ENTRY_EXISTS, ids=(run,))
            stop = _blocked(snap, now, held.mode)       # rules 1 to 3
            if stop is not None:
                raise stop
            entries = [dict(e) for e in snap[month][0]["entries"]] if month in snap else []
            for entry in entries:
                if entry["state"] == OPEN:              # open and not live: at or past expires_at
                    _abandon(entry, now)
            if held.mode == SCHEDULED:
                total = sum(e["counted_usd_micros"] for e in entries if e["mode"] == SCHEDULED)
                if total + reservation_usd_micros > cap:
                    raise Stop(OVER_CAP, ids=(month,), counts={
                        "month_total_usd_micros": total, "cap_usd_micros": cap,
                        "reservation_usd_micros": reservation_usd_micros})
            entry = {"run_id": run, "target": held.target, "mode": held.mode, "state": OPEN,
                     "kind": None, "basis": None, "reserved_usd_micros": reservation_usd_micros,
                     "counted_usd_micros": reservation_usd_micros, "estimate_usd_micros": None,
                     "opened_at": store.utc_text(now),
                     "expires_at": store.utc_text(now + timedelta(seconds=limit)),
                     "closed_at": None, "resolved_at": None}
            return month, entries + [entry], False, _admission(month, entry)

        self.expire()
        admission = self._cas(book)

        # Booked. Read every month once more and meet rules 1 to 3 again, leaving this
        # run out: an entry written to another month's object since this run's scan is
        # invisible to this month's compare-and-swap. And if all of that took until the
        # run's own deadline, no call may be sent.
        snap = self._scan()
        now = store.read_clock(self._clock)
        stop = _blocked(snap, now, held.mode, exclude=run)
        if stop is None and now >= admission.deadline_at:
            stop = Stop(ADMITTED_PAST_DEADLINE, ids=(run,))
        if stop is not None:
            self._close(admission, state=SETTLED, basis=WITHDRAWN, counted=0)
            raise stop
        return admission

    def _close(self, admitted: Admission, *, state: str, counted: int | None,
               basis: str | None = None, kind: str | None = None) -> dict:
        """Move the run's open entry to settled or unresolved. `counted` None keeps
        the reservation. The entry must be the one `admitted` describes."""
        if not isinstance(admitted, Admission):
            raise TypeError("this needs the Admission that admit() returned")
        run = admitted.run_id

        def decide(snap: dict, now: datetime, unsure: bool) -> tuple:
            found = _find(snap, run)
            if found is None:
                raise Stop(ENTRY_UNKNOWN, ids=(run,))
            month, entry = found
            if _admission(month, entry) != admitted:
                raise Stop(ADMISSION_INVALID, ids=(run,))
            figure = entry["reserved_usd_micros"] if counted is None else counted
            if entry["state"] != OPEN:
                if unsure and (entry["state"], entry["basis"], entry["kind"],
                               entry["counted_usd_micros"]) == (state, basis, kind, figure):
                    return None, None, False, dict(entry)       # an earlier try landed
                raise Stop(ENTRY_NOT_OPEN, ids=(run,))
            entries = [dict(e) for e in snap[month][0]["entries"]]
            mine = next(e for e in entries if e["run_id"] == run)
            if not _is_live(entry, now):
                # It expired, perhaps while this call was reading S3. It is abandoned
                # whoever writes it: record that in this write, then refuse.
                _abandon(mine, now)
                return month, entries, False, Stop(ENTRY_NOT_OPEN, ids=(run,))
            mine.update(state=state, basis=basis, kind=kind, counted_usd_micros=figure,
                        estimate_usd_micros=figure if state == UNRESOLVED else None,
                        closed_at=store.utc_text(now))
            return month, entries, False, dict(mine)
        return self._cas(decide)

    def settle(self, admitted: Admission, report: dict) -> dict:
        """Replace the run's reservation with what it cost: the guard's counted
        total, which is the usage Bedrock returned wherever a reply was seen and
        the worst case only where none was. An unmetered run is written as
        unresolved at that total. `admitted` is what `admit()` returned, and the
        report must be that run's own (WHOSE REPORT, module docstring). Returns
        the entry as written."""
        if not isinstance(admitted, Admission):
            raise TypeError("settle() needs the Admission that admit() returned")
        micros, unmetered = _read_report(report, admitted)
        self.expire()
        if unmetered:
            return self._close(admitted, state=UNRESOLVED, kind=UNMETERED, counted=micros)
        return self._close(admitted, state=SETTLED, basis=METERED, counted=micros)

    def mark_crashed(self, admitted: Admission) -> dict:
        """The run ended with no guard report to settle from. Its entry becomes
        unresolved at its reservation, at once."""
        if not isinstance(admitted, Admission):
            raise TypeError("mark_crashed() needs the Admission that admit() returned")
        self.expire()
        return self._close(admitted, state=UNRESOLVED, kind=CRASHED, counted=None)

    def resolve(self, run: str, basis: str, settled_usd_micros: int) -> dict:
        """The author's resolution of an unresolved entry: a settled figure and its
        basis. `upper_bound` is the Cost Explorer figure ADR-011 D7 point 4 defines;
        it cannot be checked here and is recorded as the author's statement.
        `estimate_accepted` accepts the entry's own estimate, so the figure must
        equal it, and it sets the month's "assurance waived" flag in the same write."""
        if type(basis) is not str or basis not in RESOLUTION_BASES:
            raise Stop(BASIS_UNKNOWN)
        if not _count(settled_usd_micros):
            raise Stop(SETTLED_FIGURE_INVALID)

        def decide(snap: dict, now: datetime, unsure: bool) -> tuple:
            found = _find(snap, run)
            if found is None:
                raise Stop(ENTRY_UNKNOWN, ids=(run,))
            month, entry = found
            if entry["state"] != UNRESOLVED:
                if unsure and (entry["state"], entry["basis"], entry["counted_usd_micros"]) \
                        == (RESOLVED, basis, settled_usd_micros):
                    return None, None, False, dict(entry)       # an earlier try landed
                raise Stop(ENTRY_NOT_UNRESOLVED, ids=(run,))
            if basis == ESTIMATE_ACCEPTED and settled_usd_micros != entry["estimate_usd_micros"]:
                raise Stop(ESTIMATE_DIFFERS, ids=(run,))
            entries = [dict(e) for e in snap[month][0]["entries"]]
            mine = next(e for e in entries if e["run_id"] == run)
            mine.update(state=RESOLVED, basis=basis, counted_usd_micros=settled_usd_micros,
                        resolved_at=store.utc_text(now))
            return month, entries, basis == ESTIMATE_ACCEPTED, dict(mine)

        self.expire()
        return self._cas(decide)
