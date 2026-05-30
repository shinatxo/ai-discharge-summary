# Patient v2 — second-pass patient-version generation (design sketch)

> Status: **DESIGN / for greenlight** · Author: Cowork session 2026-05-30 · Phase 3, Wave 2
> Relates to: ADR-005 future-enhancement note ("v2 second pass"), ADR-003 (residency),
> the v0.6 prompt fix (Run 4 / Ibrahim S16), and the human-in-the-loop control in the
> Model Card. No code in this document — it exists so the build can be greenlit (or not)
> with the tradeoffs on the table.

## 1. Why — the problem v2 closes

Run 4 (independent clinician review, Ibrahim, Gen Surg, scenario S16) surfaced a
"helpful hallucination": the patient version (PART C) added standard-of-care stoma
safety-netting (high-output / no-output / blockage red flags) that was **not** in the
ward-round notes. The advice was clinically sound; the problem is provenance — only
advice the responsible clinician has documented should reach the patient under the
apparent authority of the discharging team.

**v0.6 already fixes this at the prompt level** and the cold-eval regression holds 5/5
(S14–S18, including the textbook COPD and upper-GI-bleed traps). So v2 is **not urgent**.
It is worth doing as *architectural* belt-and-braces: a structural guarantee that does
not depend on the model obeying a prompt rule, plus it realises the human-in-the-loop
story already promised in the ADRs.

The single-combined-prompt design (resolved 2026-05-23, "Option A") was the right v1
call — it hits the Flesch–Kincaid ≤ 8 reading-age bar and keeps the hot path UK-only
on-demand. v2 was always recorded as the **safety** successor: *"a second pass that
regenerates the patient version from the clinician-reviewed summary — justified by
clinical safety (anchoring the leaflet to approved content), NOT by reading age."*

## 2. Core idea

Today (v1): one Bedrock call emits PART A (clinician summary) + PART B (GP letter) +
PART C (patient version), split by regex. PART C sees the **raw ward-round notes** and
can draw freely on training knowledge — which is exactly how undocumented advice leaks in.

v2: generate the patient version in a **separate, tightly-scoped pass whose only input is
the curated PART A summary — never the raw notes.** Because the second pass cannot see the
notes and is told its sole job is to *transform* an already-curated clinical summary into
patient-friendly language and *add nothing*, it is structurally unable to reintroduce
standard-of-care advice. PART A already self-polices under v0.6 (S15/S17 cold runs wrote
"no specific safety-net advice documented" into PART A unprompted), so anchoring C to A
inherits that discipline.

```
v1:  notes ──▶ [Bedrock, one call] ──▶ A + B + C            (C sees raw notes)

v2:  notes ──▶ [Bedrock call 1] ──▶ A + B
                                     │
                          A only ──▶ [Bedrock call 2: "render this summary for the
                                      patient; transform only, add nothing"] ──▶ C
```

## 3. Two implementation shapes (the decision to make)

### Option v2a — in-request two-pass (worker-side)
The worker, after producing A+B, immediately makes a second Bedrock call that takes the
**draft** PART A text and emits PART C. Flag-gated (`PATIENT_V2_SECOND_PASS`), no UI or API
change, fully backward-compatible (flag off = today's single call).

- **Pros:** small, self-contained, deployable today; no SPA work; reuses the async
  202+poll path; gives the structural guarantee immediately. A clean, reviewable increment.
- **Cons:** anchors to the *draft* A, not a clinician-*reviewed* A — so it delivers the
  architectural belt-and-braces but only the first half of the human-in-the-loop story.
- **Latency/cost:** +1 Bedrock call (~15–20 s, absorbed by async). Call 2 only emits the
  leaflet, so cap it lower (e.g. `MAX_TOKENS≈1500`) — the marginal cost is small.

### Option v2b — review-gated regeneration endpoint
A new route (e.g. `POST /generations/{id}/patient-version`) takes the clinician-**edited /
approved** summary and regenerates PART C from it. Requires an SPA affordance: show A as
editable, "Approve & generate patient leaflet" button.

- **Pros:** realises the full intent in the ADR — the leaflet is anchored to *approved*
  content, the genuine human-in-the-loop control and the strongest portfolio/safety story.
- **Cons:** multi-component build — new backend route + audit semantics for the regenerated
  leaflet + SPA edit/approve UI. Bigger than one sitting; not deployable today.

### Recommendation
Build **v2a now** as the foundational worker capability, factored as a reusable
`generate_patient_version(summary_text) -> patient_md` function and a dedicated
`patient_system_prompt.md`. Then **v2b is a thin wrapper** later: the review-gated endpoint
calls the *same* function with the clinician-approved summary instead of the draft. This
sequences the safety guarantee in today, keeps the v1 hot path untouched behind a flag, and
leaves the human-in-the-loop endpoint as a clean follow-on.

## 4. Residency (ADR-003 closure)

The second pass **must** run on `anthropic.claude-sonnet-4-6` **on-demand in eu-west-2**
(ADR-003 rule 1 — UK-only inference). Do **not** route it through a Haiku model: Haiku is
only available via the EU cross-region inference profile in-region, which would push part
of the pipeline off the strict single-region residency posture for no safety benefit. The
existing `request_region = eu-west-2` audit evidence extends to call 2 unchanged. This is
the ADR-003 point the enhancement was waiting on: v2 is designed to *preserve*, not
weaken, the residency story.

## 5. Contract for the second pass

- **Input:** PART A markdown only (curated clinician summary). The raw notes are *not*
  passed.
- **Prompt (`patient_system_prompt.md`):** a focused subset of the PART C rules from v0.6 —
  FK ≤ 8, plain English, drug names in brackets, sensitive-content handling — plus the
  hard rule: *your only source is the summary above; reproduce its advice and safety-netting
  faithfully; add no fact, advice, or "call 111/999" trigger that is not already in it; if
  no condition-specific safety-net advice is present, use only the generic fall-back line.*
- **Output:** PART C markdown only (no A/B). This **simplifies parsing** — call 2's output
  needs no regex split; call 1 only has to yield A+B.
- **Audit/hash invariants (ADR-002 held):** the worker writes `output_sha256` for the
  v2-generated patient version exactly as today; records `patient_version=v2`,
  `patient_model_version`, and `patient_parse_ok` on the GEN# row so the audit log shows
  which path produced the leaflet. Outputs table (transient) unchanged. No PHI added to
  any new field.

## 6. Test plan (cold-eval, no deploy needed to validate logic)

1. **Isolation regression:** feed each of S14–S18's PART A (from the run-2026-05-28
   outputs) into the second pass alone; confirm PART C carries only documented advice. S16
   (stoma) is the canary — it must *not* reintroduce high-output/no-output/blockage flags.
2. **Adversarial anchor test:** hand the second pass a PART A that contains *no* safety-net
   advice and verify it emits only the generic fall-back line, never invented red flags —
   the structural version of the v0.6 check.
3. **Reading age:** confirm FK stays ≤ 8 (v1 hit 3.6–6.2; transforming an already-clinical
   summary should stay in band).
4. **Equivalence on clean cases:** where PART A *does* document advice (S14 DVLA, S18 DVLA +
   activity limits), confirm v2 reproduces it without inflation — parity with v1.
5. Extend `run_cold_eval.py` with a `--patient-second-pass` mode that runs call-2 against a
   supplied summary, so this is repeatable and CI-able.

## 7. Tradeoffs summary

| Dimension | v1 (today) | v2a (proposed) |
|---|---|---|
| Helpful-hallucination defence | prompt rule (v0.6) | prompt rule **+ architecture** |
| Anchored to | raw notes | curated PART A |
| Human-in-the-loop (approved content) | no | partial (draft A); full under v2b |
| Bedrock calls | 1 | 2 (call 2 small-token) |
| Latency | ~20–25 s | ~+15–20 s (async-absorbed) |
| Residency | UK-only on-demand | UK-only on-demand (unchanged) |
| Parsing | 3-way regex split | call 2 needs no split |
| Hot-path risk | n/a | none — flag-gated, off by default |

## 8. Out of scope today
- v2b SPA edit/approve UI and the regeneration endpoint (follow-on; v2a is built to make it thin).
- Any change to PART A / PART B generation.
- Multi-region or Haiku-via-EU-profile routing (explicitly rejected on residency grounds, §4).
