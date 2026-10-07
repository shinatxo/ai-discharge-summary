"""evals/run_cold_eval.py — the W4 v1 baseline harness (ADR-009 "Build record — W4").

No network: Bedrock is a fake client. Scenario IDs only in test names and
messages — never note text."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

import run_cold_eval as rce  # noqa: E402
import safety_net_gate  # noqa: E402

V1_LINE = safety_net_gate.V1_LINES[0]
CORPUS, CORPUS_SHA = rce.load_corpus(rce.DEFAULT_CORPUS_PATH)
ALL_IDS = [s.id for s in CORPUS]


def london(y, m, d, h, mi):
    return dt.datetime(y, m, d, h, mi, tzinfo=rce.LONDON)


QUIET = london(2026, 10, 7, 14, 0)      # a Wednesday afternoon


def _note_lines(min_len=15):
    return [ln.strip() for s in CORPUS for ln in s.notes.splitlines() if len(ln.strip()) >= min_len]


class FakeBedrock:
    """Returns a well-formed combined output and a well-formed leaflet. With
    echo_notes, PART A repeats the notes — as a real model output can — so the
    console-hygiene test has something to leak if the runner printed output.
    invent_in_a_for: notes for which PART A's advice field carries an invented
    trigger — on the deployed path PART C is replaced by the leaflet, so PART A
    is where an invention must sit to reach the gate (the S15/S18 shape)."""

    def __init__(self, throttles=0, fail_code=None, echo_notes=False, invent_in_a_for=()):
        self.calls = []
        self.throttles = throttles
        self.fail_code = fail_code
        self.echo_notes = echo_notes
        self.invent_in_a_for = set(invent_in_a_for)

    def converse(self, **kw):
        self.calls.append(kw)
        if self.throttles:
            self.throttles -= 1
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "Converse")
        if self.fail_code:
            raise ClientError({"Error": {"Code": self.fail_code, "Message": "bad"}}, "Converse")
        user = kw["messages"][0]["content"][0]["text"]
        if kw["inferenceConfig"]["maxTokens"] == rce.PATIENT_MAX_TOKENS:
            text = f"PART C - PATIENT VERSION\n\nYou were in hospital.\n\n{V1_LINE}\n"
            usage = {"inputTokens": 3000, "outputTokens": 800,
                     "cacheReadInputTokens": 1200, "cacheWriteInputTokens": 0}
        else:
            echo = f"\n{user}\n" if self.echo_notes else ""
            advice = ("If the pain returns, call 999." if user in self.invent_in_a_for
                      else "Not documented")
            text = (f"PART A - CLINICIAN SUMMARY\nDIAGNOSIS: test{echo}\n"
                    f"PATIENT ADVICE: {advice}\n\n"
                    f"PART B - GP LETTER\nDear GP\n\n"
                    f"PART C - PATIENT VERSION\nYou were in hospital.\n{V1_LINE}\n")
            usage = {"inputTokens": 5500, "outputTokens": 2300,
                     "cacheReadInputTokens": 0, "cacheWriteInputTokens": 4800}
        return {"output": {"message": {"content": [{"text": text}]}},
                "usage": usage, "stopReason": "end_turn"}


def _main(tmp_path, argv, fake=None, now=QUIET, confirm=lambda _: "yes"):
    fake = fake if fake is not None else FakeBedrock()
    code = rce.main(argv, client_factory=lambda: fake, now=lambda: now,
                    sleep=lambda _s: None, confirm=confirm, runs_dir=tmp_path)
    return code, fake


# --- corpus and prompts -------------------------------------------------------

def test_corpus_is_the_eighteen_canary_scenarios_in_file_order():
    raw = json.loads(rce.DEFAULT_CORPUS_PATH.read_text(encoding="utf-8"))["scenarios"]
    assert ALL_IDS == [s["id"] for s in raw]
    assert len(ALL_IDS) == 18


def test_notes_are_sent_as_the_canary_sends_them_stripped_and_unfenced():
    raw = {s["id"]: s["notes"] for s in json.loads(rce.DEFAULT_CORPUS_PATH.read_text())["scenarios"]}
    for s in CORPUS:
        assert s.notes == raw[s.id].strip(), s.id
        assert not s.notes.startswith("```"), s.id


def test_notes_sha256_is_the_dispatchers_input_sha256():
    import hashlib
    for s in CORPUS:
        assert s.notes_sha256 == hashlib.sha256(s.notes.encode("utf-8")).hexdigest(), s.id


def test_corpus_rejects_duplicate_and_empty(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"scenarios": [{"id": "X1", "notes": "a"}, {"id": "X1", "notes": "b"}]}))
    with pytest.raises(ValueError, match="duplicate"):
        rce.load_corpus(p)
    p.write_text(json.dumps({"scenarios": [{"id": "X1", "notes": "  \n"}]}))
    with pytest.raises(ValueError, match="empty"):
        rce.load_corpus(p)


@pytest.mark.parametrize("ids,all_,msg", [
    ([], False, "--all"), (["S1"], True, "not both"), (["S99"], False, "not in the corpus"),
    (["S1", "S1"], False, "repeated"),
])
def test_scenario_selection_errors(ids, all_, msg):
    with pytest.raises(ValueError, match=msg):
        rce.select_scenarios(CORPUS, ids, all_)


def test_default_prompt_is_the_one_the_worker_ships_and_matches_the_prompts_copy():
    assert rce.DEFAULT_PROMPT_PATH == ROOT / "src" / "generate" / "system_prompt.md"
    copy = ROOT / "prompts" / "discharge-summary-system-prompt.md"
    assert rce.load_system_prompt(rce.DEFAULT_PROMPT_PATH) == rce.load_system_prompt(copy)


# --- canary windows -----------------------------------------------------------

@pytest.mark.parametrize("start,minutes,clash", [
    (QUIET, 90, False),
    (london(2026, 10, 7, 1, 45), 5, True),       # inside the nightly window
    (london(2026, 10, 7, 0, 50), 60, True),      # runs into it
    (london(2026, 10, 7, 2, 21), 30, False),     # just after it closes
    (london(2026, 10, 12, 3, 10), 5, True),      # Monday's weekly run
    (london(2026, 10, 13, 3, 10), 5, False),     # Tuesday 03:10 is clear
    (london(2026, 10, 25, 1, 40), 5, True),      # clocks go back that night
    (london(2026, 12, 7, 1, 40), 5, True),       # GMT
])
def test_canary_window(start, minutes, clash):
    got = rce.canary_window_clash(start, start + dt.timedelta(minutes=minutes))
    assert (got is not None) is clash


def test_refuses_a_batch_that_would_run_into_a_window(tmp_path):
    code, fake = _main(tmp_path, ["--all", "--runs", "5", "--deployed-path", "--yes"],
                       now=london(2026, 10, 7, 0, 30))
    assert code == 3 and not fake.calls and not any(tmp_path.iterdir())


# --- cost ---------------------------------------------------------------------

def test_cost_prices_every_token_type_with_the_regional_premium():
    calls = [{"input_tokens": 1_000_000, "output_tokens": 0},
             {"input_tokens": 0, "output_tokens": 1_000_000,
              "cache_read_tokens": 1_000_000, "cache_write_tokens": 1_000_000}]
    assert rce.cost_usd(calls) == pytest.approx((3 + 15 + 0.30 + 3.75) * 1.10)


def test_missing_usage_counts_as_zero():
    assert rce.cost_usd([{"input_tokens": None, "output_tokens": None}]) == 0


# --- one generation -------------------------------------------------------------

def _cfg(second_pass=True, caching=True):
    sp = rce.load_system_prompt(rce.DEFAULT_PROMPT_PATH)
    pp = rce.load_system_prompt(rce.DEFAULT_PATIENT_PROMPT_PATH) if second_pass else None
    return rce.Config(sp, rce._prompt_meta(rce.DEFAULT_PROMPT_PATH, sp), pp,
                      rce._prompt_meta(rce.DEFAULT_PATIENT_PROMPT_PATH, pp) if pp else None,
                      caching, True)


def test_deployed_path_makes_two_calls_records_both_and_gates_with_v1_lines():
    s = CORPUS[0]
    fake = FakeBedrock()
    rec = rce.run_generation(fake, s, _cfg(), batch_id="b", run=1, started_at="t")
    assert [c["name"] for c in rec["calls"]] == ["combined", "patient_v2"]
    assert rec["path"] == "deployed" and rec["patient_version"] == "v2"
    assert "(v2 second pass)" in rec["output"]
    assert rec["gate"]["canonical"] == "V1_LINES" and rec["gate"]["ok"], rec["gate"]["status"]
    assert all("cachePoint" in str(c["system"]) for c in fake.calls)
    assert fake.calls[0]["messages"][0]["content"][0]["text"] == s.notes   # exact bytes
    assert rec["cost_usd_est"] == rce.cost_usd(rec["calls"]) > 0
    json.dumps(rec, allow_nan=False)


def test_combined_path_makes_one_call_without_cache_point():
    fake = FakeBedrock()
    rec = rce.run_generation(fake, CORPUS[0], _cfg(False, False), batch_id="b", run=1, started_at="t")
    assert len(fake.calls) == 1 and rec["path"] == "combined" and rec["patient_version"] == "v1"
    assert "cachePoint" not in str(fake.calls[0]["system"])


def test_throttling_is_retried_at_most_twice():
    rec = rce.run_generation(FakeBedrock(throttles=2), CORPUS[0], _cfg(), batch_id="b",
                             run=1, started_at="t", sleep=lambda _s: None)
    assert rec["error"] is None and rec["calls"][0]["throttle_retries"] == 2
    rec = rce.run_generation(FakeBedrock(throttles=3), CORPUS[0], _cfg(), batch_id="b",
                             run=1, started_at="t", sleep=lambda _s: None)
    assert rec["error"]["code"] == "ThrottlingException" and rec["gate"] is None


def test_other_errors_are_recorded_not_retried():
    fake = FakeBedrock(fail_code="ValidationException")
    rec = rce.run_generation(fake, CORPUS[0], _cfg(), batch_id="b", run=1, started_at="t")
    assert len(fake.calls) == 1 and rec["error"]["code"] == "ValidationException"


def test_record_does_not_copy_the_notes():
    rec = rce.run_generation(FakeBedrock(), CORPUS[0], _cfg(), batch_id="b", run=1, started_at="t")
    text = json.dumps(rec)
    # Counts only: pytest's introspection would print a list of note lines on failure.
    n_copied = sum(1 for ln in CORPUS[0].notes.splitlines() if len(ln.strip()) >= 15 and ln.strip() in text)
    assert n_copied == 0


# --- the batch ------------------------------------------------------------------

def test_dry_run_creates_no_client_and_writes_nothing(tmp_path, capsys):
    called = []
    code = rce.main(["--all", "--runs", "5", "--deployed-path", "--dry-run"],
                    client_factory=lambda: called.append(1), now=lambda: QUIET, runs_dir=tmp_path)
    assert code == 0 and not called and not any(tmp_path.iterdir())
    out = capsys.readouterr().out
    assert "90 generations" in out and "180 Bedrock calls" in out


def test_confirmation_required_unless_yes(tmp_path):
    code, fake = _main(tmp_path, ["S1"], confirm=lambda _: "no")
    assert code == 1 and not fake.calls and not any(tmp_path.iterdir())


def test_full_batch_layout_and_tier1_5_met(tmp_path):
    code, fake = _main(tmp_path, ["--all", "--runs", "2", "--deployed-path", "--batch", "b1", "--yes"])
    assert code == 0 and len(fake.calls) == 18 * 2 * 2
    b = tmp_path / "b1"
    manifest = json.loads((b / "manifest.json").read_text())
    assert manifest["corpus"]["sha256"] == CORPUS_SHA and len(manifest["scenarios"]) == 18
    for run in ("r1", "r2"):
        assert (b / run / "SUMMARY.md").exists()
        for sid in ALL_IDS:
            rec = json.loads((b / run / f"{sid}.json").read_text())
            assert rec["generation_id"] == f"b1/{run}/{sid}"
            assert (b / run / f"{sid}.md").exists()
    index = json.loads((b / "batch.json").read_text())
    assert index["tier1_5_met"] is True and len(index["generations"]) == 36
    assert "**MET**" in (b / "BATCH_SUMMARY.md").read_text()


def test_one_gate_failure_means_tier1_5_not_met(tmp_path):
    target = next(s for s in CORPUS if not safety_net_gate.notes_document_seek_help(s.notes))
    fake = FakeBedrock(invent_in_a_for=[target.notes])
    code, _ = _main(tmp_path, ["--all", "--deployed-path", "--batch", "b2", "--yes"], fake=fake)
    assert code == 1
    index = json.loads((tmp_path / "b2" / "batch.json").read_text())
    assert index["tier1_5_met"] is False
    failed = [g["scenario_id"] for g in index["generations"] if g["gate_ok"] is False]
    assert failed == [target.id]


def test_fewer_than_eighteen_scenarios_never_meets_tier1_5(tmp_path):
    code, _ = _main(tmp_path, ["S1", "S2", "--deployed-path", "--batch", "b3", "--yes"])
    assert code == 0
    assert json.loads((tmp_path / "b3" / "batch.json").read_text())["tier1_5_met"] is False


def test_never_overwrites_a_batch(tmp_path):
    (tmp_path / "b4").mkdir()
    code, fake = _main(tmp_path, ["S1", "--batch", "b4", "--yes"])
    assert code == 2 and not fake.calls


def test_stops_cleanly_if_a_window_is_about_to_open(tmp_path):
    # Start 01:00 (clear: 18 x 50 s ends 01:15). Two generations run, then the
    # clock reads 01:29:30 — the next one would end inside the 01:30 opening.
    late = london(2026, 10, 7, 1, 29) + dt.timedelta(seconds=30)
    times = iter([london(2026, 10, 7, 1, 0)] * 3 + [late] * 100)
    fake = FakeBedrock()
    code = rce.main(["--all", "--batch", "b5", "--yes"], client_factory=lambda: fake,
                    now=lambda: next(times), sleep=lambda _s: None, runs_dir=tmp_path)
    assert code == 1
    index = json.loads((tmp_path / "b5" / "batch.json").read_text())
    assert index["stopped_early"] is True and index["tier1_5_met"] is False
    assert 0 < len(index["generations"]) < 18


def test_console_never_shows_note_text_output_or_findings(tmp_path, capsys):
    target = next(s for s in CORPUS if not safety_net_gate.notes_document_seek_help(s.notes))
    fake = FakeBedrock(echo_notes=True, invent_in_a_for=[target.notes])
    _main(tmp_path, ["--all", "--deployed-path", "--batch", "b6", "--yes"], fake=fake)
    captured = capsys.readouterr()
    console = captured.out + captured.err
    # Counts and booleans only: on failure pytest must not print the leaked text.
    n_leaked = sum(1 for ln in _note_lines() if ln in console)
    invented_shown = "call 999" in console.lower()
    pinned_shown = V1_LINE in console
    assert n_leaked == 0
    assert not invented_shown
    assert not pinned_shown
