"""D4 medication reconciliation — the second step-level metric (ADR-009 (e)).

Scored IDENTICALLY on v1's PART A "MEDICATIONS ON DISCHARGE" section and on
step 5a's reconciliation list, against the same gold (evals/gold/). Each
generator is reduced to a list of Entries; scoring only ever sees Entries.

The rubric is EVAL_RESULTS.md D4, made deterministic, with the CSO decisions
of 6 Oct 2026:

  FAIL     a documented discharge drug missing; a pre-admission drug silently
           dropped; an invented dose or frequency (the notes give none, the
           output gives one); a wrong dose or frequency; a drug the gold says must
           not appear (inpatient-only, stopped, not prescribed); a STOPPED drug
           shown as continuing; a conflict the notes leave open, resolved to one
           value (B6); drugs listed where the notes document none or do not address
           discharge medication (reconstruction); an extra entry the CSO has ruled
           invented.
  PARTIAL  every drug, dose and frequency right, but a change tag missing or
           imprecise (INCREASED without the old dose; NEW for a frequency change;
           STOPPED for WITHHELD).
  PASS     otherwise.
  REVIEW   otherwise passing, with an entry naming no gold drug that the CSO has
           not yet ruled on (a synonym the gold lacks, or an invented drug).

Tolerance (decision 1c): a DH drug the notes do not list at discharge
(`inferred_from_dh`) passes as continued, as flagged for confirmation, or as
covered by a section-level "confirm against the TTO" statement. It fails if
dropped silently or shown changed.

Results carry indices, drug names from the GOLD, counts and fixed labels —
never output text.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
_DOSE = re.compile(r"(?<![\w/])(\d+(?:\.\d+)?)\s*(mg|mcg|micrograms?|g|units?|ml|iu)\b", re.I)
_UNIT = {"micrograms": "mcg", "microgram": "mcg", "units": "unit", "iu": "unit"}

# frequency surface forms -> canonical code
_FREQ = [
    (r"\bOD\b|\bonce (?:a )?daily\b|\bonce a day\b|\bmane\b|(?<!twice )(?<!times )\bdaily\b", "OD"),
    (r"\bBD\b|\btwice (?:a )?daily\b|\btwice a day\b", "BD"),
    (r"\bTDS\b|\bthree times (?:a )?da(?:il)?y\b", "TDS"),
    (r"\bQDS\b|\bfour times (?:a )?da(?:il)?y\b", "QDS"),
    (r"\bON\b|\bnocte\b|\bat night\b", "ON"),
    (r"\bPRN\b|\bas required\b|\bas needed\b", "PRN"),
    (r"\bregular(?:ly)?\b", "REGULAR"),
    (r"\bweekly\b", "WEEKLY"),
]
_FREQ = [(re.compile(p, re.I if code not in ("OD", "BD", "ON") else 0), code) for p, code in _FREQ]
_FREQ_GOLD = {"od": "OD", "daily": "OD", "bd": "BD", "tds": "TDS", "qds": "QDS", "on": "ON",
              "prn": "PRN", "regular": "REGULAR", "weekly": "WEEKLY"}

# change tags, by earliest position in the entry ("continued; held on admission"
# is continued; "WITHHELD on discharge" is withheld)
_TAGS = [
    (re.compile(r"\bSTOPPED\b|\bdiscontinued\b|\bstopped\b"), "stopped"),
    (re.compile(r"\bWITHHELD\b|\bwithheld\b|\bwithhold\b|\bsuspended\b|\bon hold\b"), "withheld"),
    (re.compile(r"\bINCREASED\b|\bincreased\b|\buptitrated\b"), "increased"),
    (re.compile(r"\bDECREASED\b|\bdecreased\b|\breduced\b"), "decreased"),
    (re.compile(r"\bNEW\b|\bnew\b|\bstarted\b|\bcommenced\b"), "new"),
    (re.compile(r"\bcontinued\b|\bcontinue\b|\bunchanged\b|\bCONTINUED\b"), "continued"),
    (re.compile(r"\bchanged\b|\bCHANGED\b|\bswitched\b"), "changed"),
]
_CONFIRM = re.compile(r"\b(confirm|check|verify|reconcil\w*|clarif\w*)\b", re.I)
_CONFLICT = re.compile(r"\b(discrepan\w*|conflict\w*|contradict\w*|differ\w*|disagree\w*|"
                       r"inconsisten\w*|two different|query|clarif\w*)\b", re.I)
_INPATIENT = re.compile(r"\b(inpatient|completed|course complete|not (?:for|on) discharge|"
                        r"stopped|STOPPED|discontinued|not continued)\b", re.I)
_SECTION_CONFIRM = re.compile(r"(confirm|check|reconcile|verify)[^.\n]{0,60}\b(TTO|TTA|discharge "
                              r"(?:medication|prescription|list)|regular medication|remaining)", re.I)
_NONE = re.compile(r"^\W*(none|nil|no (?:discharge )?medications?(?: required| needed)?)\b", re.I)
_NOT_DOC = re.compile(r"\bnot documented\b|\bnot recorded\b", re.I)


def dose_tokens(text: str) -> set[str]:
    out = set()
    for num, unit in _DOSE.findall(text):
        n = num.rstrip("0").rstrip(".") if "." in num else num
        out.add(f"{n}{_UNIT.get(unit.lower(), unit.lower())}")
    return out


def norm_dose(dose: str | None) -> str | None:
    if dose is None:
        return None
    toks = dose_tokens(dose)
    return next(iter(toks)) if len(toks) == 1 else None    # "per INR", "high dose" -> non-numeric


def freq_tokens(text: str) -> set[str]:
    return {code for rx, code in _FREQ if rx.search(text)}


def tag_of(text: str) -> str | None:
    best = None
    for rx, tag in _TAGS:
        m = rx.search(text)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), tag)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------
@dataclass
class Entry:
    text: str                  # the entry as written (v1) or rendered (5a) — never logged
    doses: set[str]
    freqs: set[str]
    tag: str | None
    confirm_flag: bool
    conflict_flag: bool


@dataclass
class MedView:
    generator: str
    entries: list[Entry]
    section_found: bool
    says_none: bool
    says_not_documented: bool
    section_confirm: bool
    warning_text: str = ""     # clinician-warning blocks (scanned for must-not names only)


def _entry(text: str) -> Entry:
    return Entry(text, dose_tokens(text), freq_tokens(text), tag_of(text),
                 bool(_CONFIRM.search(text) or _NOT_DOC.search(text)), bool(_CONFLICT.search(text)))


_SECTION_HEAD = re.compile(r"^[\s#*>]*MEDICATIONS ON DISCHARGE\b.*$", re.I | re.M)
_NEXT_HEAD = re.compile(r"^[\s#*>]*(?:[A-Z][A-Z &/()'\-]{3,}:?)\s*(?:$|:)", re.M)
_WARN = re.compile(r"^\s*(?:⚠|\*\*⚠|CLINICIAN (?:REVIEW|ACTION|NOTE)|NOTE\b|Note:)", re.I)
_SUBHEAD_SKIP = re.compile(r"^\s*(pre-?admission|drugs? accounted|previous|admission medication|"
                           r"inpatient)", re.I)
_SUBHEAD = re.compile(r"^\s*[A-Za-z][^:|│]{2,60}:\s*$")
_ITEM = re.compile(r"^\s*(?:\d{1,2}[.)]|[-*•])\s+")
_BOX = re.compile(r"[│┃┆|]")
_RULE_ROW = re.compile(r"^[\s|│┌┐└┘├┤┬┴┼─━=:\-+]*$")


def med_section(part_a: str) -> str | None:
    m = _SECTION_HEAD.search(part_a)
    if not m:
        return None
    rest = part_a[m.end():]
    # The section ends at the next ALL-CAPS field heading (ALLERGIES, FOLLOW-UP, …),
    # but not at a warning line, which stays inside it.
    for h in _NEXT_HEAD.finditer(rest):
        line = h.group(0).strip()
        if not line.startswith(("⚠", "CLINICIAN", "NOTE")) and not _BOX.search(line):
            return rest[:h.start()]
    return rest


_STATEMENT = re.compile(r"^\W*(?:none|nil|n/a)\b|^\W*no (?:discharge |regular )?(?:medications?|meds|drugs)\b|"
                        r"^\W*(?:discharge medications?:?\s*)?not documented\b", re.I)
_INLINE_SKIP = re.compile(r"^\s*(?:pre-?admission[^:]{0,40}|drugs? accounted[^:]{0,20}|"
                          r"admission medications?)\s*:", re.I)
_BOX_ONLY = re.compile(r"^[\s|│┃┌┐└┘├┤┬┴┼─━═║╔╗╚╝╠╣╦╩╬=:\-+]*$")


def _cells(s: str) -> list[str]:
    s = s.strip()
    if s[:1] in "|│┃":
        s = s[1:]
    if s[-1:] in "|│┃":
        s = s[:-1]
    return [c.strip() for c in _BOX.split(s)]


def _split_entries(section: str) -> tuple[list[str], list[str], str]:
    """(entry texts, statement texts, warning text). Table rows, list items and
    their wrapped continuation lines become entries. Pre-admission sub-lists and
    clinician-warning blocks do not; "None." / "Not documented" lines are
    statements about the list, not entries."""
    entries: list[str] = []
    statements: list[str] = []
    warn: list[str] = []
    mode = "entries"
    for raw in section.splitlines():
        line = raw.rstrip()
        s = line.strip()
        if not s:
            if mode in ("warn", "skip", "skip_line"):
                mode = "entries"
            continue
        if _BOX_ONLY.match(s):
            continue
        if _WARN.match(s):
            mode = "warn"
        if mode == "warn":
            warn.append(s)
            continue
        if _INLINE_SKIP.match(s):
            mode = "skip_line"            # this line and its wrapped continuation only
            continue
        if mode == "skip_line":
            if raw[:1].isspace() or s[:1].islower():
                continue
            mode = "entries"
        if _SUBHEAD.match(s) and not _BOX.search(s):
            mode = "skip" if _SUBHEAD_SKIP.match(s) else "entries"
            continue
        if mode == "skip":                # a pre-admission sub-list, to the next blank line
            continue
        if _BOX.search(s):
            cells = _cells(s)
            if any(c.lower() in ("drug", "medication") for c in cells) and len(cells) > 1:
                continue                                     # header row
            if cells and not cells[0] and entries:          # wrapped box row
                entries[-1] += " " + " ".join(c for c in cells if c)
            else:
                entries.append(" ".join(c for c in cells if c))
            continue
        body = _ITEM.sub("", s) if _ITEM.match(s) else s
        # A plain line saying "confirm against the TTO" is about the list; a numbered
        # item that says it is still that drug's entry.
        if _STATEMENT.match(body) or (_SECTION_CONFIRM.search(body) and not _ITEM.match(s)):
            statements.append(body)
            continue
        if _ITEM.match(s):
            entries.append(body)
        elif entries and raw[:1].isspace():
            entries[-1] += " " + s                              # wrapped list item
        else:
            entries.append(s)
    return [e for e in entries if e.strip()], statements, "\n".join(warn)


def view_from_v1(combined: str) -> MedView:
    import safety_net_gate as gate
    part_a, _ = gate.split_parts(combined)
    section = med_section(part_a)
    if section is None:
        return MedView("v1", [], False, False, False, False)
    texts, statements, warn = _split_entries(section)
    says_none = any(re.match(r"^\W*(none|nil)\b|^\W*no (?:discharge )?(?:medications?|meds)\b"
                             r"(?! documented)", s, re.I) for s in statements)
    says_nd = any(_NOT_DOC.search(s) for s in statements)
    return MedView("v1", [_entry(x) for x in texts], True, says_none, says_nd,
                   bool(_SECTION_CONFIRM.search(section)), warn)


def view_from_5a(reconciliation: list[dict], discharge_status: str | None = None) -> MedView:
    """Step 5a's list — per drug {drug, dose, route, frequency, tag, ...} (ADR-009
    step table). Rendered to the same Entry form; nothing is parsed."""
    entries = []
    for d in reconciliation:
        text = " ".join(str(x) for x in (d.get("drug"), d.get("dose"), d.get("route"),
                                          d.get("frequency"), d.get("tag")) if x)
        e = _entry(text)
        e.tag = (d.get("tag") or "").lower() or None
        entries.append(e)
    return MedView("pipeline", entries, True, discharge_status == "none_required",
                   discharge_status == "not_documented", False)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _names(d: dict) -> list[str]:
    return [d["drug"], *d["aliases"]]


def _first_pos(text: str, names: list[str]) -> int | None:
    pos = [m.start() for n in names
           for m in [re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text, re.I)] if m]
    return min(pos) if pos else None


def _owners(entries: list[Entry], meds: dict) -> dict[int, tuple[str, str]]:
    """Each entry belongs to the gold drug (or must-not drug) it names FIRST —
    "Paracetamol … replacing ibuprofen" is a paracetamol entry, not ibuprofen's."""
    cands = [("drug", d["drug"], _names(d)) for d in meds["drugs"]] + \
            [("must_not", m["drug"], _names(m)) for m in meds["must_not_appear"]]
    owner = {}
    for i, e in enumerate(entries):
        best = None
        for kind, name, names in cands:
            p = _first_pos(e.text, names)
            if p is not None and (best is None or p < best[0]):
                best = (p, kind, name)
        if best:
            owner[i] = (best[1], best[2])
    return owner


def entry_key(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text.strip().lower()).encode("utf-8")).hexdigest()


def _freq_code(f: str | None) -> str | None:
    return _FREQ_GOLD.get(f.lower()) if f else None


def score(view: MedView, gold: dict, adjudications: dict | None = None) -> dict:
    adjudications = adjudications or {}
    meds = gold["medications"]
    status = meds["discharge_status"]
    findings: list[dict] = []          # {"drug", "kind", "severity"}
    used: set[int] = set()

    def find(severity, kind, drug=None):
        findings.append({"drug": drug, "kind": kind, "severity": severity})

    if not view.section_found:
        find("fail", "section_missing")

    owner = _owners(view.entries, meds)
    for d in meds["drugs"]:
        names = _names(d)
        hits = [i for i, o in owner.items() if o == ("drug", d["drug"])]
        used.update(hits)
        entries = [view.entries[i] for i in hits]
        name = d["drug"]
        if not entries:
            if d["basis"] == "inferred_from_dh" and view.section_confirm:
                continue
            find("fail", "dropped" if d["dh_cites"] else "missing", name)
            continue
        text = " ".join(e.text for e in entries)
        doses = set().union(*(e.doses for e in entries))
        freqs = set().union(*(e.freqs for e in entries))
        tags = [e.tag for e in entries if e.tag]
        tag = tags[0] if tags else None

        # conflict (B6): both values, or an explicit flag; never one value alone
        if "conflict" in d:
            vals = {norm_dose(v) for v in d["conflict"]["values"]}
            shown = vals & doses
            flagged = any(e.conflict_flag for e in entries)
            if len(shown) < len(vals) and not flagged:
                find("fail", "conflict_resolved", name)
            continue_dose_check = False
        else:
            continue_dose_check = True

        allowed = set()
        if d["previous"] and d["previous"].get("dose"):
            allowed.add(norm_dose(d["previous"]["dose"]))
        gold_dose = norm_dose(d["dose"])
        if continue_dose_check:
            if gold_dose:
                allowed.add(gold_dose)
                if gold_dose not in doses:
                    find("fail", "dose_wrong_or_missing", name)
            extra = {t for t in doses if t not in allowed}
            if extra:
                find("fail", "dose_invented" if not gold_dose else "dose_wrong_or_missing", name)

        gold_freq = _freq_code(d["frequency"])
        prev_freq = _freq_code((d["previous"] or {}).get("frequency"))
        if gold_freq and gold_freq not in freqs:
            find("fail", "frequency_wrong_or_missing", name)
        if not gold_freq:
            extra_f = freqs - {prev_freq}
            if extra_f:
                find("fail", "frequency_invented", name)

        gt = d["tag"]
        if d["basis"] == "inferred_from_dh":
            if tag not in (None, "continued") and not any(e.confirm_flag for e in entries):
                find("fail", "inferred_changed", name)
            continue
        if gt == "stopped":
            if tag in ("continued", "new", "increased", "decreased"):
                find("fail", "stopped_shown_continuing", name)
            elif tag != "stopped":
                find("partial", "tag_imprecise", name)
        elif gt == "withheld":
            if tag in ("continued", "new", "increased", "decreased"):
                find("fail", "withheld_shown_continuing", name)
            elif tag != "withheld":
                find("partial", "tag_imprecise", name)
        elif gt in ("increased", "decreased"):
            if tag != gt:
                find("partial", "tag_imprecise", name)
            elif d["previous"].get("dose") and norm_dose(d["previous"]["dose"]) not in doses:
                find("partial", "previous_dose_missing", name)
        elif gt == "changed":
            mentions_prev = prev_freq is not None and prev_freq in freqs
            if tag != "changed" and not mentions_prev:
                find("partial", "tag_imprecise", name)
        elif gt in ("new", "continued"):
            if tag != gt:
                find("partial", "tag_imprecise", name)

    for m in meds["must_not_appear"]:
        for i, o in owner.items():
            if o == ("must_not", m["drug"]):
                used.add(i)
                if not _INPATIENT.search(view.entries[i].text):
                    find("fail", "must_not_appear", m["drug"])

    if status in ("none_required", "not_documented"):
        active = [i for i, e in enumerate(view.entries)
                  if i not in used or e.tag not in ("stopped", "withheld")]
        listed_gold = [d for d in meds["drugs"] if d["tag"] != "stopped"]
        if active and not listed_gold:
            find("fail", "drugs_where_none_documented")

    extras = []
    for i, e in enumerate(view.entries):
        if i in used:
            continue
        ruling = adjudications.get(entry_key(e.text), {}).get("verdict")
        kind = {"invented": "adjudicated_invented", "acceptable": "adjudicated_acceptable"}.get(
            ruling, "unadjudicated")
        extras.append({"index": i, "key": entry_key(e.text), "kind": kind})
        if kind == "adjudicated_invented":
            find("fail", "invented_drug")

    sev = {f["severity"] for f in findings}
    if "fail" in sev:
        verdict = "fail"
    elif "partial" in sev:
        verdict = "partial"
    elif any(x["kind"] == "unadjudicated" for x in extras):
        verdict = "review"
    else:
        verdict = "pass"
    n_gold = len(meds["drugs"])
    wrong = {f["drug"] for f in findings if f["severity"] == "fail" and f["drug"]}
    return {
        "generator": view.generator,
        "gold_status": gold["status"],
        "discharge_status": status,
        "verdict": verdict,
        "findings": findings,
        "drugs": {"gold": n_gold, "fully_correct": n_gold - len(wrong & {d["drug"] for d in meds["drugs"]})},
        "invented_doses": sum(1 for f in findings if f["kind"] == "dose_invented"),
        "invented_frequencies": sum(1 for f in findings if f["kind"] == "frequency_invented"),
        "extras": extras,
        "n_entries": len(view.entries),
    }
