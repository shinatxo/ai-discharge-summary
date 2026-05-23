# `generate` Lambda

Bedrock-calling function for the AI Discharge Summary Assistant (Phase 2, slice 2).

- `app.py` — handler. Calls Bedrock Converse on the pinned model, splits the
  combined output into PART A/B/C, and writes a **hash-only** audit item
  (ADR-002). Never stores or logs PHI.
- `system_prompt.md` — **synced copy** of the canonical
  `prompts/discharge-summary-system-prompt.md` (v0.5). The repo root prompt is
  the source of truth; re-sync after any prompt change:

  ```bash
  cp prompts/discharge-summary-system-prompt.md src/generate/system_prompt.md
  ```

  (A future hardening is to load the prompt from S3 / SSM Parameter Store at
  runtime so there is a single source, versioned and access-controlled.)
- `requirements.txt` — empty by design; boto3 ships with the runtime.

## Event contract (direct invoke; HTTP front door is a later slice)

```json
{
  "notes": "<free-text ward-round notes>",
  "user_sub": "<cognito subject — pseudonymous id>",
  "output_type": "summary,gp_letter,patient"
}
```

Returns the three drafts plus `generation_id`, `draft: true`, and the SHA-256
hashes that were written to the audit table.
