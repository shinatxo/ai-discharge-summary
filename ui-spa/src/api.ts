import { fetchAuthSession } from 'aws-amplify/auth';
import { apiBase } from './amplify-config';

/**
 * Thin client for the slice-4b HTTP API. Two calls:
 *
 *   - postGenerate(notes, idempotencyKey) -> 202 + job_id (or 200 on replay)
 *   - getStatus(job_id)                   -> pending | complete | failed | expired
 *
 * Identity is the user's Cognito IdToken (NOT the access token — Cognito puts
 * the App Client id in `aud` only on the IdToken, and the API Gateway JWT
 * authoriser checks `aud` against the App Client id).
 */

export interface DispatchResponse {
  ok: boolean;
  job_id: string;
  status: 'pending' | 'complete' | 'failed' | 'expired';
  poll_url: string;
  started_at?: string;
  input_sha256?: string;
  idempotent_replay?: boolean;
  message?: string;
  error?: string;
}

export interface StatusResponse {
  ok: boolean;
  job_id: string;
  status: 'pending' | 'complete' | 'failed' | 'expired';
  started_at?: string;
  completed_at?: string;
  failed_at?: string;
  draft?: boolean;
  model_version?: string;
  input_sha256?: string;
  parse_ok?: boolean;
  outputs?: { summary: string; gp_letter: string; patient: string };
  output_sha256?: Record<string, string>;
  error?: string;
  error_code?: string;
  error_message?: string;
}

async function authHeader(): Promise<string> {
  // fetchAuthSession returns the cached session if it's still valid, and
  // silently refreshes it using the refresh token if it's not. The IdToken
  // is the one with `aud` set to the App Client id.
  const session = await fetchAuthSession();
  const idToken = session.tokens?.idToken?.toString();
  if (!idToken) {
    throw new Error('not_signed_in');
  }
  return `Bearer ${idToken}`;
}

/** Generate a fresh UUID for the Idempotency-Key header. */
export function newIdempotencyKey(): string {
  // crypto.randomUUID is available in every browser Vite supports.
  // Falls back to a manual UUIDv4 if a downlevel target is ever introduced.
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  // RFC 4122 v4 fallback
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export async function postGenerate(
  notes: string,
  idempotencyKey: string,
): Promise<DispatchResponse> {
  const res = await fetch(`${apiBase}/generate`, {
    method: 'POST',
    headers: {
      'authorization': await authHeader(),
      'content-type': 'application/json',
      'idempotency-key': idempotencyKey,
    },
    body: JSON.stringify({ notes }),
  });
  const body = (await res.json()) as DispatchResponse;
  if (!res.ok && res.status !== 202 && res.status !== 200) {
    throw Object.assign(new Error(body.error || `http_${res.status}`), {
      status: res.status,
      body,
    });
  }
  return body;
}

export async function getStatus(jobId: string): Promise<StatusResponse> {
  const res = await fetch(`${apiBase}/generations/${encodeURIComponent(jobId)}`, {
    method: 'GET',
    headers: {
      'authorization': await authHeader(),
    },
  });
  // 404 is a real outcome here (unknown / cross-user). Let the caller decide.
  const body = (await res.json()) as StatusResponse;
  if (!res.ok && res.status !== 404) {
    throw Object.assign(new Error(body.error || `http_${res.status}`), {
      status: res.status,
      body,
    });
  }
  if (res.status === 404) {
    return { ok: false, job_id: jobId, status: 'failed', error: 'not_found' };
  }
  return body;
}
