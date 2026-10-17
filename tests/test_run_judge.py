"""evals/run_judge.py — the rubric v2 judge runner (W5).

No network: Bedrock is a fake client. Scenario IDs only in test names and
messages — never note text."""

from __future__ import annotations

import datetime as dt
import json
import sys
import threading
import time
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

import gold as G  # noqa: E402
import make_blind_sheets as mbs  # noqa: E402
import run_cold_eval as rce  # noqa: E402
import run_judge as rj  # noqa: E402

CORPUS, CORPUS_SHA = rce.load_corpus(rce.DEFAULT_CORPUS_PATH)
BY_ID = {s.id: s for s in CORPUS}
IDS = ("S1", "S3")
OUTPUT = "PART A - SUMMARY\nAdmitted 01/05/2025. Allergies: NKDA.\nPART C - PATIENT VERSION\nGo home.\n"
QUIET = dt.datetime(2026, 10, 14, 14, 0, tzinfo=rce.LONDON)       # a Wednesday afternoon


def L(sid):
    return G.line_index(BY_ID[sid].notes)


def first_line(sid):
    lid, text = next((k, v) for k, v in L(sid).items() if len(v.strip()) > 20)
    return lid, text.strip()[:30]


def judgement(sid="S1", **dims):
    base = {d: {"verdict": "pass", "auto_fail": False, "findings": []} for d in rj.DIMENSIONS}
    for d, v in dims.items():
        base[d] = v
    return {"dimensions": base}


def finding(sid="S1", quote="Admitted 01/05/2025.", cites=None):
    if cites is None:
        lid, q = first_line(sid)
        cites = [{"lines": [lid], "quote": q}]
    return {"output_quote": quote, "note_cites": cites, "reason": "added year"}


# --- verification ---------------------------------------------------------------
def test_verified_finding_is_kept():
    j = judgement(D2={"verdict": "fail", "auto_fail": False, "findings": [finding(cites=[])]})
    v = rj.verify(j, L("S1"), OUTPUT)
    assert v["defects"] == []
    assert v["dimensions"]["D2"]["evidence_ok"] is True and len(v["dimensions"]["D2"]["findings"]) == 1


def test_output_quote_not_in_output_is_discarded():
    j = judgement(D6={"verdict": "fail", "auto_fail": False,
                      "findings": [finding(quote="words that are not there", cites=[])]})
    v = rj.verify(j, L("S1"), OUTPUT)
    assert v["dimensions"]["D6"]["discarded"] == [{"index": 0, "reason": "output_quote_not_found"}]
    assert v["dimensions"]["D6"]["evidence_ok"] is False


def test_note_cite_that_fails_verify_cite_is_discarded():
    j = judgement(D7={"verdict": "fail", "auto_fail": False,
                      "findings": [finding(cites=[{"lines": ["L001"], "quote": "zzz not in notes zzz"}])]})
    v = rj.verify(j, L("S1"), OUTPUT)
    assert v["dimensions"]["D7"]["discarded"][0]["reason"] == "note_cite_quote_not_found"


def test_whitespace_and_curly_quotes_are_normalised_like_step_3():
    j = judgement(D2={"verdict": "partial", "auto_fail": False,
                      "findings": [finding(quote="Admitted   01/05/2025.", cites=[])]})
    assert rj.verify(j, L("S1"), OUTPUT)["dimensions"]["D2"]["evidence_ok"] is True


@pytest.mark.parametrize("dim, value, reason", [
    ("D2", {"verdict": "na", "auto_fail": False, "findings": []}, "na_not_allowed"),
    ("D3", {"verdict": "pass", "auto_fail": True, "findings": []}, "auto_fail_without_fail"),
    ("D6", {"verdict": "maybe", "auto_fail": False, "findings": []}, "bad_verdict"),
])
def test_rule_breaches_are_defects(dim, value, reason):
    v = rj.verify(judgement(**{dim: value}), L("S1"), OUTPUT)
    assert {"dimension": dim, "reason": reason} in v["defects"]


def test_missing_dimension_and_non_dict():
    j = judgement()
    del j["dimensions"]["D8"]
    assert {"dimension": "D8", "reason": "missing_dimension"} in rj.verify(j, {}, OUTPUT)["defects"]
    assert len(rj.verify("nonsense", {}, OUTPUT)["defects"]) == len(rj.DIMENSIONS)


def test_na_allowed_for_d6_and_d8():
    v = rj.verify(judgement(D8={"verdict": "na", "auto_fail": False, "findings": []}), L("S1"), OUTPUT)
    assert v["defects"] == [] and v["dimensions"]["D8"]["evidence_ok"] is True


# --- prompt, schema, freeze --------------------------------------------------------
def test_system_prompt_is_instructions_then_section_2():
    system, meta = rj.load_prompt()
    assert "## SYSTEM PROMPT" not in system
    assert system.rstrip().endswith("</rubric>") and "<rubric>\n## 2. Scoring rubric" in system
    assert meta["rubric_sha256"] == mbs.rubric_sha256()


def test_strict_schema_every_object_closed_and_fully_required():
    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for p in node["properties"].values():
                walk(p)
        if node.get("type") == "array":
            walk(node["items"])
    s = rj.tool_schema()
    walk(s)
    assert set(s["properties"]["dimensions"]["properties"]) == set(rj.DIMENSIONS)
    assert rj.tool_config()["toolChoice"] == {"tool": {"name": rj.TOOL_NAME}}
    # Bedrock refuses strict mode for this schema (grammar too large) - not sent
    assert "strict" not in rj.tool_config()["tools"][0]["toolSpec"]


def test_freeze_then_check(tmp_path):
    _, meta = rj.load_prompt()
    fp = tmp_path / "FROZEN.json"
    rj.freeze(meta, fp, QUIET)
    assert rj.check_frozen(meta, fp)["rubric_sha256"] == meta["rubric_sha256"]
    with pytest.raises(rj.JudgeError, match="already exists"):
        rj.freeze(meta, fp, QUIET)
    changed = dict(meta, rubric_sha256="0" * 64)
    with pytest.raises(rj.JudgeError, match="rubric changed"):
        rj.check_frozen(changed, fp)


# --- the runner -----------------------------------------------------------------
class FakeBedrock:
    def __init__(self, reply=None, throttles=0, stop="tool_use", delay=0.0):
        self.reply, self.throttles, self.stop, self.delay = reply, throttles, stop, delay
        self.calls, self.active, self.max_active = [], 0, 0
        self._lock = threading.Lock()

    def converse(self, **req):
        with self._lock:
            self.calls.append(req)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            throttle = self.throttles > 0
            if throttle:
                self.throttles -= 1
        try:
            time.sleep(self.delay)
            if throttle:
                raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "Converse")
            reply = self.reply if self.reply is not None else judgement()
            return {"stopReason": self.stop,
                    "output": {"message": {"content": [
                        {"toolUse": {"toolUseId": "t1", "name": rj.TOOL_NAME, "input": reply}}]}},
                    "usage": {"inputTokens": 4000, "outputTokens": 500,
                              "cacheReadInputTokens": 5000, "cacheWriteInputTokens": 0}}
        finally:
            with self._lock:
                self.active -= 1


def make_batch(tmp_path, ids=IDS, runs=(1,)):
    b = tmp_path / "batch"
    gens = []
    for run in runs:
        for sid in ids:
            rec = {"generation_id": f"tb/r{run}/{sid}", "scenario_id": sid, "run": run,
                   "generator": "v1", "notes_sha256": BY_ID[sid].notes_sha256,
                   "output": OUTPUT, "error": None}
            p = b / f"r{run}" / f"{sid}.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(rec))
            gens.append({"run": run, "scenario_id": sid, "record": f"r{run}/{sid}.json"})
    (b / "manifest.json").write_text(json.dumps({"batch_id": "tb", "generator": "v1",
                                                 "corpus": {"sha256": CORPUS_SHA}}))
    (b / "batch.json").write_text(json.dumps({"batch_id": "tb", "generations": gens}))
    return b


@pytest.fixture
def env(tmp_path):
    fp = tmp_path / "FROZEN.json"
    _, meta = rj.load_prompt()
    rj.freeze(meta, fp, QUIET)
    return {"batch": make_batch(tmp_path), "frozen": fp, "cal": tmp_path / "calibration"}


def run_main(env, argv, client=None, at=QUIET, confirm=lambda _: "yes"):
    return rj.main([str(env["batch"])] + argv, client_factory=(lambda: client) if client else None,
                   now=lambda: at, sleep=lambda _: None, confirm=confirm,
                   frozen_path=env["frozen"], calibration_root=env["cal"])


def test_refuses_when_not_frozen(env, capsys):
    env["frozen"].unlink()
    assert run_main(env, ["--dry-run"]) == 2
    assert "not frozen" in capsys.readouterr().err


def test_refuses_while_blind_scoring_is_unfinished(env, capsys):
    mbs.build(env["batch"], 1, env["cal"] / "tb-r1")
    assert run_main(env, ["--dry-run"]) == 2
    assert "blind scoring of r1 is not finished (0/2)" in capsys.readouterr().err


def test_dry_run_creates_no_client_and_writes_nothing(env):
    assert run_main(env, ["--dry-run"]) == 0
    assert not (env["batch"] / "judge").exists()


def test_refuses_a_canary_window(env, capsys):
    at = dt.datetime(2026, 10, 14, 1, 50, tzinfo=rce.LONDON)
    assert run_main(env, ["--dry-run"], at=at) == 3


def test_live_run_writes_pinned_records(env):
    fake = FakeBedrock()
    assert run_main(env, ["--judge-id", "j1", "--repeats", "2"], fake) == 0
    out = env["batch"] / "judge" / "j1"
    m = json.loads((out / "manifest.json").read_text())
    assert (m["model_id"], m["temperature"], m["repeats"]) == (rj.MODEL_ID, 0.0, 2)
    assert m["prompt"]["rubric_sha256"] == mbs.rubric_sha256()
    rec = json.loads((out / "rep2" / "r1" / "S3.json").read_text())
    assert rec["error"] is None and rec["verified"]["defects"] == []
    assert len(fake.calls) == 4
    req = fake.calls[0]
    assert req["system"][-1] == {"cachePoint": {"type": "default"}}
    assert req["inferenceConfig"] == {"maxTokens": rj.MAX_TOKENS, "temperature": 0.0}
    assert "<notes>\nL001: " in req["messages"][0]["content"][0]["text"]


def test_never_more_than_four_in_flight(env):
    env["batch"] = make_batch(env["batch"].parent / "b2", ids=("S1", "S2", "S3", "S4", "S8", "S9"))
    fake = FakeBedrock(delay=0.02)
    assert run_main(env, ["--judge-id", "j2", "--concurrency", "4"], fake) == 0
    assert fake.max_active <= 4 and len(fake.calls) == 6
    assert run_main(env, ["--judge-id", "j3", "--concurrency", "5", "--dry-run"]) == 2


def test_throttle_is_retried_then_succeeds(env):
    fake = FakeBedrock(throttles=1)
    assert run_main(env, ["--judge-id", "j4", "--concurrency", "1"], fake) == 0
    assert len(fake.calls) == 3


def test_max_tokens_is_an_error_not_a_judgement(env):
    assert run_main(env, ["--judge-id", "j5"], FakeBedrock(stop="max_tokens")) == 1
    rec = json.loads((env["batch"] / "judge" / "j5" / "rep1" / "r1" / "S1.json").read_text())
    assert rec["error"] == {"code": "max_tokens"} and rec["judgement"] is None


def test_refuses_to_overwrite(env):
    assert run_main(env, ["--judge-id", "j6"], FakeBedrock()) == 0
    assert run_main(env, ["--judge-id", "j6"], FakeBedrock()) == 2


def test_console_shows_no_note_text_output_text_or_verdicts(env, capsys):
    reply = judgement(D2={"verdict": "fail", "auto_fail": False, "findings": [finding(cites=[])]})
    assert run_main(env, ["--judge-id", "j7"], FakeBedrock(reply=reply)) == 0
    text = capsys.readouterr()
    printed = text.out + text.err
    for sid in IDS:
        for ln in BY_ID[sid].notes.splitlines():
            if len(ln.strip()) >= 15:
                assert ln.strip() not in printed
    for word in ("Admitted", "added year", '"fail"', "verdict"):
        assert word not in printed


class FailingBedrock(FakeBedrock):
    def converse(self, **req):
        self.calls.append(req)
        raise ClientError({"Error": {"Code": "ValidationException",
                                     "Message": "The compiled grammar is too large"}}, "Converse")


def test_provider_error_message_is_recorded_in_the_file_not_printed(env, capsys):
    assert run_main(env, ["--judge-id", "j8"], FailingBedrock()) == 1
    rec = json.loads((env["batch"] / "judge" / "j8" / "rep1" / "r1" / "S1.json").read_text())
    assert rec["error"] == {"code": "ValidationException", "message": "The compiled grammar is too large"}
    assert rec["tool_strict"] is False
    out = capsys.readouterr()
    assert "grammar" not in out.out + out.err and "ERROR ValidationException" in out.out
