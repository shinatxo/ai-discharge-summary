"""evals/score_safety_net.py + evals/score_batch.py — safety-netting correctness,
the first step-level metric (WS1a DoD 5; ADR-009 (e) as amended 1 and 6 Oct 2026).

Synthetic notes only. Scenario IDs only in test names and messages."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src" / "generate"))
sys.path.insert(0, str(ROOT / "tests"))

import gold as G  # noqa: E402
import run_cold_eval as rce  # noqa: E402
import safety_net_gate as gate  # noqa: E402
import score_batch as SB  # noqa: E402
import score_safety_net as S  # noqa: E402
from pipeline import run_step  # noqa: E402

CORPUS = {s.id: s for s in rce.load_corpus(rce.DEFAULT_CORPUS_PATH)[0]}
V1 = gate.V1_CANONICAL
RUN_0915 = ROOT / "evals" / "runs" / "run-2026-09-15-discharge-summary-system-prompt-patient-v2"


def v1_output(advice="Not documented", part_c=V1, extra_a=""):
    return (f"PART A - CLINICIAN SUMMARY\nDIAGNOSIS: test\n{extra_a}\n"
            f"PATIENT ADVICE\n{advice}\n\nFOLLOW-UP\nNone.\n\n"
            f"PART B - GP LETTER\nDear GP\n\nPART C - PATIENT VERSION\nYou were in hospital.\n{part_c}\n")


def score_v1(sid, out, adjudications=None):
    s = CORPUS[sid]
    return S.score(S.view_from_v1(out), G.load(sid), G.line_index(s.notes), adjudications)


# --- (i)-(iii) on synthetic v1 outputs ------------------------------------------

def test_fallback_scenario_with_not_documented_and_the_pinned_line_passes():
    r = score_v1("S1", v1_output())
    assert r["verdict"] == "pass" and r["route"]["predicted"] == "fallback"


def test_invented_trigger_in_part_a_fails_route_and_invention():
    r = score_v1("S1", v1_output(advice="If the chest pain comes back, call 999."))
    assert set(r["failures"]) >= {"route", "invented_trigger"}


def test_the_v06_shape_documented_advice_but_invented_trigger_in_c_fails():
    # S15: advice documented (smoking cessation), no seek-help trigger. The v0.6 failure
    # added "if breathlessness worsens or sputum changes, call 111" — still an invention.
    out = v1_output(advice="Smoking cessation discussed.",
                    part_c=f"If your breathlessness worsens or your sputum changes, call NHS 111.\n{V1}")
    r = score_v1("S15", out)
    assert r["route"]["correct"] and r["failures"] == ["invented_trigger"]


def test_documented_seek_help_allows_a_trigger_beyond_the_pinned_line():
    out = v1_output(advice="Safety-net advice given to parents about fever, feeding and breathing.",
                    part_c="If feeding drops, call NHS 111.")
    r = score_v1("S8", out)
    assert r["verdict"] == "pass" and r["seek_help"]["n_invented"] == 0 and not r["pinned"]["required"]


def test_pinned_line_missing_on_fallback_fails():
    r = score_v1("S1", v1_output(part_c="Take your medicines as prescribed."))
    assert "pinned_line_missing" in r["failures"]


def test_meta_statements_are_flags_not_advice():
    r = score_v1("S1", v1_output(advice="Not documented — please add condition-appropriate "
                                        "safety-netting before issuing."))
    assert r["route"]["predicted"] == "fallback" and r["advice"]["meta_flags"] >= 1


def test_list_markers_do_not_become_sentences():
    view = S.view_from_v1(v1_output(advice="1. No swimming alone.\n2. Must not drive.\n3. Must inform the DVLA."))
    assert view.advice_units == ["No swimming alone.", "Must not drive.", "Must inform the DVLA."]


def test_unlocated_sentence_goes_to_review_then_follows_the_cso_ruling():
    out = v1_output(advice="No swimming alone. Avoid caffeine for a week.")
    r = score_v1("S18", out)
    assert r["verdict"] == "review" and r["advice"]["n_unlocated"] == 1
    key = next(u["key"] for u in r["advice"]["units"] if u["kind"] == "unlocated")
    assert score_v1("S18", out, {key: {"verdict": "unsupported"}})["failures"] == ["unsupported_advice"]
    assert score_v1("S18", out, {key: {"verdict": "supported"}})["verdict"] == "pass"


def test_route_basis_separates_record_only_advice():
    assert score_v1("S8", v1_output())["route"]["gold_basis"] == "record_only"
    assert score_v1("S9", v1_output())["route"]["gold_basis"] == "instruction"   # "phone support"
    assert score_v1("S18", v1_output())["route"]["gold_basis"] == "instruction"
    assert score_v1("S1", v1_output())["route"]["gold_basis"] is None


def test_results_carry_no_output_text():
    out = v1_output(advice="No swimming alone. Avoid caffeine for a week.")
    text = json.dumps(score_v1("S18", out))
    assert "swimming" not in text and "caffeine" not in text


# --- the parser on real v0.7 output (15 Sep, deployed path) --------------------

@pytest.mark.parametrize("sid,verdict,predicted,unlocated", [
    ("S8", "pass", "documented", 0),      # the advice record, reproduced
    ("S9", "fail", "fallback", 0),        # v0.7 calls the education record "Not documented" (pre-1 Oct)
    ("S15", "pass", "documented", 0),     # advice carried, no trigger invented
    ("S18", "review", "documented", 3),   # three paraphrases for the CSO
])
def test_real_v07_outputs(sid, verdict, predicted, unlocated):
    out = (RUN_0915 / f"{sid}.md").read_text(encoding="utf-8").split("## OUTPUT", 1)[1]
    r = score_v1(sid, out)
    assert (r["verdict"], r["route"]["predicted"], r["advice"]["n_unlocated"]) == (verdict, predicted, unlocated)
    assert r["seek_help"]["n_invented"] == 0


# --- the same metric on step 5b's real output ------------------------------------

PROBE = json.loads((ROOT / "tests" / "fixtures" / "step3_probe_20261001.json").read_text())["facts"]


def _5b(sid, facts=None):
    flags = run_step("guard_input", {"notes": CORPUS[sid].notes})["flags"]
    return run_step("select_safety_net", {"facts": facts or PROBE[sid], "flags": flags})


@pytest.mark.parametrize("sid", sorted(PROBE))
def test_5b_real_output_scores_against_the_same_gold(sid):
    s = CORPUS[sid]
    r = S.score(S.view_from_5b(_5b(sid)), G.load(sid), G.line_index(s.notes))
    assert r["verdict"] == "pass" and r["pinned"]["audience_ok"] is True


def test_5b_wrong_audience_fails():
    out = dict(_5b("S8"))
    out["pinned_line"] = gate.CANONICAL_ADULT
    r = S.score(S.view_from_5b(out), G.load("S8"), G.line_index(CORPUS["S8"].notes))
    assert "wrong_audience" in r["failures"]


# --- the batch ------------------------------------------------------------------

def _batch(tmp_path, fake):
    code = rce.main(["--all", "--deployed-path", "--batch", "b", "--yes"], client_factory=lambda: fake,
                    now=lambda: rce.dt.datetime(2026, 10, 7, 14, 0, tzinfo=rce.LONDON),
                    sleep=lambda _s: None, runs_dir=tmp_path)
    return tmp_path / "b", code


class _Fake:
    """Combined call: PART A says 'Not documented'; the leaflet carries the v1 line."""
    def converse(self, **kw):
        if kw["inferenceConfig"]["maxTokens"] == rce.PATIENT_MAX_TOKENS:
            text = f"PART C\nYou were in hospital.\n{V1}\n"
        else:
            text = v1_output()
        return {"output": {"message": {"content": [{"text": text}]}},
                "usage": {"inputTokens": 1, "outputTokens": 1}, "stopReason": "end_turn"}


def test_batch_scores_and_reports(tmp_path, capsys):
    b, _ = _batch(tmp_path, _Fake())
    capsys.readouterr()
    assert SB.main([str(b)]) == 0
    scored = json.loads((b / "scores" / "safety_net.json").read_text())
    assert len(scored["results"]) == 18
    by = {r["scenario_id"]: r for r in scored["results"]}
    # "Not documented" everywhere: right on the 8 fall-back scenarios, wrong on the 10 documented.
    assert sum(r["route"]["correct"] for r in by.values()) == 8
    assert all(by[s]["verdict"] == "pass" for s in ("S1", "S12", "S13", "S16", "S17", "A5", "B6", "C7"))
    summary = (b / "scores" / "SAFETY_NET.md").read_text()
    assert "8/18" in summary and "Provisional" not in summary
    console = capsys.readouterr().out
    assert "Not documented" not in console and V1 not in console


def test_batch_refuses_a_changed_corpus(tmp_path):
    b, _ = _batch(tmp_path, _Fake())
    m = json.loads((b / "manifest.json").read_text())
    m["corpus"]["sha256"] = "0" * 64
    (b / "manifest.json").write_text(json.dumps(m))
    assert SB.main([str(b)]) == 2


def test_wilson_interval():
    lo, hi = SB.wilson(90, 90)
    assert hi == pytest.approx(1.0) and 0.95 < lo < 0.97      # rule-of-three territory
    assert SB.wilson(0, 0) == (0.0, 0.0)
