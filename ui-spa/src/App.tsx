import { useState } from 'react';
import type { AuthUser } from 'aws-amplify/auth';

import { GenerateForm } from './components/GenerateForm';
import { JobStatus } from './components/JobStatus';

interface AppProps {
  signOut?: () => void;
  user?: AuthUser;
}

/**
 * Slice 4b shell. Two states:
 *
 *   1. No job yet -> show the notes form.
 *   2. A job_id has been received from POST /generate -> show the JobStatus
 *      panel, which polls GET /generations/{id} until terminal.
 *
 * After a terminal state we let the user "Generate another" which clears the
 * job_id and returns to state (1). Each new submission gets a FRESH
 * Idempotency-Key so it isn't deduplicated against the previous one.
 */
export function App({ signOut, user }: AppProps) {
  const [jobId, setJobId] = useState<string | null>(null);

  return (
    <div className="page">
      <header className="topbar">
        <h1>AI Discharge Summary Assistant</h1>
        <div className="topbar-meta">
          <span className="who">{user?.signInDetails?.loginId ?? user?.username}</span>
          <button className="signout" onClick={signOut}>Sign out</button>
        </div>
      </header>

      <p className="banner">
        <strong>Portfolio demo — not a medical device.</strong> Use only synthetic
        data. Every output is a draft requiring clinician review and sign-off.
      </p>

      {jobId === null ? (
        <GenerateForm onJobAccepted={setJobId} />
      ) : (
        <JobStatus jobId={jobId} onReset={() => setJobId(null)} />
      )}

      <footer className="footer">
        Slice 4b · async 202&nbsp;+&nbsp;poll · region eu-west-2 · single-region by ADR-003
      </footer>
    </div>
  );
}
