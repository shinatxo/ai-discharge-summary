import { useEffect, useRef, useState } from 'react';

import { getStatus, type StatusResponse } from '../api';
import { OutputTabs } from './OutputTabs';

interface Props {
  jobId: string;
  onReset: () => void;
}

/**
 * Polls GET /generations/{jobId} until status is terminal (complete, failed,
 * or expired). Polling cadence ramps from 1s up to 4s to avoid hammering the
 * status endpoint during a longer Bedrock run, but stays responsive in the
 * common (warm) case where the job completes in ~20s.
 *
 *   Backoff: 1s, 1.5s, 2s, 2.5s, 3s, 3.5s, 4s, 4s, 4s, ...
 *
 * A hard ceiling of 5 minutes is enforced — if we haven't seen a terminal
 * state by then something is genuinely wrong (Lambda async retries have an
 * upper bound of ~6h but for a UI 5 minutes is plenty).
 */
export function JobStatus({ jobId, onReset }: Props) {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [elapsedSec, setElapsedSec] = useState(0);
  const startedAt = useRef<number>(Date.now());
  const cancelled = useRef(false);

  useEffect(() => {
    cancelled.current = false;
    let attempt = 0;
    const HARD_CEILING_MS = 5 * 60 * 1000;
    const tick = async () => {
      if (cancelled.current) return;
      try {
        const next = await getStatus(jobId);
        if (cancelled.current) return;
        setStatus(next);
        setElapsedSec(Math.floor((Date.now() - startedAt.current) / 1000));
        if (next.status === 'complete' || next.status === 'failed'
            || next.status === 'expired') {
          return; // terminal; stop polling
        }
      } catch (e) {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const err = e as any;
        setError(err?.body?.message || err?.message || 'status check failed');
      }
      if (Date.now() - startedAt.current > HARD_CEILING_MS) {
        setError('still pending after 5 minutes — give up and try again');
        return;
      }
      attempt += 1;
      const delayMs = Math.min(1000 + attempt * 500, 4000);
      setTimeout(tick, delayMs);
    };
    // First fetch is immediate (no initial delay; the 202 has only just landed
    // but the worker may have completed by now if Bedrock was hot).
    void tick();
    return () => { cancelled.current = true; };
  }, [jobId]);

  const isTerminal = status && (status.status === 'complete'
                                || status.status === 'failed'
                                || status.status === 'expired');

  return (
    <div className="card">
      <div className="job-header">
        <div>
          <h2>Generation status</h2>
          <p className="muted">job: <code>{jobId}</code></p>
        </div>
        <button className="secondary" onClick={onReset}>
          {isTerminal ? 'Generate another' : 'Cancel & start new'}
        </button>
      </div>

      <p>
        Elapsed: <strong>{elapsedSec}s</strong>
        {' · '}
        State:{' '}
        <span className={`badge badge-${status?.status ?? 'pending'}`}>
          {status?.status ?? 'pending'}
        </span>
        {status?.model_version && (
          <>{' · '}<span className="muted">{status.model_version}</span></>
        )}
      </p>

      {error && <p className="error">{error}</p>}

      {status?.status === 'pending' && (
        <p className="muted">
          Worker is running the Bedrock call. The dispatcher returned 202 in &lt;1s;
          this poll cycle is what shows you the generation progress. (No 30s
          cap, no ghost rows.)
        </p>
      )}

      {status?.status === 'complete' && status.outputs && (
        <OutputTabs
          outputs={status.outputs}
          hashes={status.output_sha256}
          parseOk={status.parse_ok}
          inputSha256={status.input_sha256}
        />
      )}

      {status?.status === 'failed' && (
        <div className="error-box">
          <p><strong>Generation failed.</strong></p>
          <p>Error code: <code>{status.error_code}</code></p>
          {status.error_message && (
            <p className="muted">{status.error_message}</p>
          )}
          <p className="muted">
            This row is recorded in the audit log with status=failed — the
            audit log captures attempts, not just successes (slice 4a finding).
          </p>
        </div>
      )}

      {status?.status === 'expired' && (
        <div className="error-box">
          <p><strong>This draft has expired.</strong></p>
          <p className="muted">
            The transient outputs are kept for 24h then auto-cleaned by TTL.
            The audit trail (hashes only) survives. Please regenerate.
          </p>
        </div>
      )}
    </div>
  );
}
