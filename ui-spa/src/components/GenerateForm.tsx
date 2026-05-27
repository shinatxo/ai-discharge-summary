import { useState } from 'react';

import { postGenerate, newIdempotencyKey } from '../api';

interface Props {
  onJobAccepted: (jobId: string) => void;
}

/**
 * The notes textarea + submit. Notable behaviours:
 *
 *  - The submit button DISABLES on click and stays disabled while the POST is
 *    in flight. This is the slice-4a "ghost record" mitigation at the UI
 *    layer — even with an idempotency key, we still don't want the user to
 *    spam-click and fire three POSTs in 100ms.
 *  - A single Idempotency-Key is generated PER SUBMISSION. If the user gets
 *    a network error and clicks again, the SAME key is reused (so the retry
 *    is idempotent at the dispatcher). On "Generate another" the key is
 *    regenerated so it isn't deduped against the previous job.
 */
export function GenerateForm({ onJobAccepted }: Props) {
  const [notes, setNotes] = useState('');
  const [idemKey, setIdemKey] = useState<string>(() => newIdempotencyKey());
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const result = await postGenerate(notes, idemKey);
      if (!result.job_id) {
        throw new Error(result.message || result.error || 'no job_id in response');
      }
      onJobAccepted(result.job_id);
    } catch (e) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const err = e as any;
      // Keep the message short and PHI-free.
      setError(err?.body?.message || err?.message || 'failed to submit');
      setSubmitting(false);
    }
  };

  return (
    <form className="card" onSubmit={submit}>
      <label htmlFor="notes" className="label">
        Ward-round notes <span className="muted">(synthetic data only)</span>
      </label>
      <textarea
        id="notes"
        className="notes"
        rows={14}
        value={notes}
        onChange={(e) => setNotes(e.target.value)}
        placeholder={'e.g. 72M NSTEMI on troponin, PMH HTN/T2DM. Started ticagrelor 90mg BD ...'}
        required
      />
      <div className="row">
        <div className="key">
          <span className="muted">Idempotency-Key:</span>
          <code title="A POST retry with the same key returns the same job_id and does NOT re-invoke Bedrock.">{idemKey}</code>
          <button type="button" className="link"
            onClick={() => setIdemKey(newIdempotencyKey())}
            title="Generate a new key (treat the next submission as a fresh request).">
            new key
          </button>
        </div>
        <button type="submit" className="primary" disabled={submitting || !notes.trim()}>
          {submitting ? 'Submitting…' : 'Generate drafts'}
        </button>
      </div>
      {error && <p className="error">{error}</p>}
    </form>
  );
}
