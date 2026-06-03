# Case study — AI Discharge Summary Assistant

Two versions below: a short **LinkedIn post** ready to paste, and a longer **written case study** for a blog or portfolio page. Both lead with the same honest hook — a synthetic canary catching a bug the unit tests couldn't.

Live: https://discharge.shinaoguntoye.dev · Code: https://github.com/shinatxo/ai-discharge-summary

---

## LinkedIn post (short)

I built a synthetic-traffic canary for my clinical-AI side project. On its first night, it caught a bug my unit tests never could.

I'm a doctor teaching myself cloud engineering, and my flagship build is an **AI Discharge Summary Assistant** on AWS — it turns messy ward-round notes into a discharge summary, a GP letter, and a plain-English patient leaflet. The hard part of clinical AI isn't fluency, it's *restraint*: not inventing the drug, the resus status, or the safety advice that wasn't in the notes.

So I wrapped it in real engineering: a fully serverless stack (Bedrock, Lambda, Cognito, an async 202/poll API so generation isn't bound by API Gateway's 30s cap), a **hash-only audit log** that never stores patient data, and a **tamper-evident WORM ledger** so the audit trail is provable, not aspirational.

Then I added a canary: a scheduled Lambda that replays all 18 test scenarios through the *live* system every night and alarms on the results.

Night one, it failed — only 1–2 of 18 succeeded. Not a model problem: jobs were silently stuck. The canary had surfaced that my generate worker's Lambda timeout (60s) was shorter than a real generation (50–80s once I'd added a second safety pass). The fix was one line. But my unit tests, which mock Bedrock, would *never* have caught it — only synthetic traffic against the real path did.

That's the lesson I keep relearning moving from medicine to engineering: observability isn't dashboards, it's the thing that tells you you're wrong before a user does.

Studying for AWS SAA-C03. Build + writeup in the comments. 👇

#AWS #CloudEngineering #Serverless #HealthTech #Observability

---

## Written case study (long)

### From the ward to the cloud

I'm a medical doctor who started teaching myself to code at the end of 2024. The discharge summary is a piece of paperwork I wrote hundreds of times as a junior doctor — and watched delay GP follow-up and contribute to readmissions when it was done badly. It's the perfect problem for a healthcare-flavoured cloud portfolio: a real clinical pain point, a genuine safety bar, and a reason to do the engineering properly.

### The actual hard problem: restraint, not fluency

Modern language models write a fluent discharge summary easily. The dangerous part is what they *add*: a medication the notes didn't mention, a resuscitation status nobody documented, "standard" safety advice the responsible clinician never gave. In medicine, a confidently-stated fact that isn't in the record is not a helpful flourish — it's a hazard.

So the whole system is engineered around restraint. It writes **"Not documented"** wherever the notes are silent, so gaps are visible to the reviewing clinician instead of being papered over. Every output is a **draft for sign-off** — the clinician stays the author of record. And the safety rules are tested against ~18 synthetic scenarios (neonatal to elderly, medical and surgical, plus adversarial cases like prompt injection and internally contradictory notes), with auto-fail gates for invented drugs, resus status, and diagnoses.

### The architecture, and why each piece is there

It's fully serverless on AWS, in eu-west-2 for UK data residency:

- **Cognito + an API Gateway JWT authoriser** verify identity before any Lambda runs, and the handler reads the user from the *verified* token claim — never the request body — so the audit log can't be spoofed.
- An **async 202 + poll** pattern: Bedrock generation takes ~50–80s, past API Gateway's hard 30s cap, so a dispatcher returns a job id in under a second and the client polls a status endpoint. Client-supplied idempotency keys mean a retried request never double-bills a Bedrock call.
- A **hash-only DynamoDB audit log** (it stores SHA-256 hashes and metadata, never patient data) whose stream feeds an **S3 Object Lock (WORM) ledger** — append-only and undeletable within its retention window. That's what turns "we keep an immutable audit log" from a claim into something provable.

### When a clinician found what my evals missed

I sent specialty-matched review packs to eleven practising doctors. One replied with substance — and caught something my automated evals had passed: on a surgical case, the patient leaflet had added standard stoma safety-netting (high-output / no-output / blockage warnings). Good advice. But it *wasn't in the ward-round notes*. Sound advice the responsible clinician didn't document is still a hallucinated instruction to the patient.

I fixed it twice. At the prompt level, an explicit "no model-added clinical advice" rule (verified by a cold regression across textbook traps the model used to fall for). And at the architecture level — **Patient v2** — the patient leaflet is now regenerated in a separate model pass whose only input is the *curated clinician summary*, so it's structurally unable to reintroduce undocumented advice no matter what the prompt says. Defence in depth.

### The canary that earned its keep on night one

The piece I'm proudest of is the least glamorous: a **synthetic-traffic canary**. A scheduled Lambda signs in as a dedicated test user and replays all 18 scenarios through the live, deployed system every night — the same Cognito → API → worker → Bedrock path a real clinician hits — and publishes success-rate, latency, and throttle metrics that CloudWatch alarms watch, with an SNS email if something drifts.

The first time it ran, it failed: 1–2 of 18 succeeded, the rest stuck. It wasn't the model. The canary had exposed two things my unit tests structurally could not:

1. My generate worker's Lambda **timeout was 60 seconds** — but a real generation, especially once I'd added the Patient v2 second pass, took 50–80s. Longer cases were being killed mid-flight and left stuck `pending`. The async design meant the worker had no gateway cap to worry about, so the fix was simply raising the timeout. A latent reliability bug, invisible to tests that mock Bedrock, caught by traffic against the real thing.
2. Firing 18 generations at once **saturated the account's Bedrock quota**. I reshaped the canary to a bounded-concurrency sliding window — more like real traffic — which also told me exactly what quota increase to request.

Observability, it turns out, isn't dashboards. It's the discipline of building something that tells you you're wrong before a user does.

### What I'd tell a hiring manager

I tried to build this the way I'd want production healthcare software built: least-privilege IAM scoped to single ARNs, encryption with customer-managed keys, an audit trail you can't quietly edit, governance docs (model card, STRIDE threat model) treated as deliverables — and a repo that documents what *didn't* work, because a portfolio of only green ticks isn't credible. The clinical context drove the architecture decisions; the engineering raised the bar on the clinical problem. That combination is the whole point.

Built while studying for the AWS Solutions Architect Associate (SAA-C03).

**Live:** https://discharge.shinaoguntoye.dev (sign-in gated — demo on request) · **Code & full writeup:** https://github.com/shinatxo/ai-discharge-summary
