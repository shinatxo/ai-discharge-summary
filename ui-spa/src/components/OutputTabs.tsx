import { useState } from 'react';

interface Props {
  outputs: { summary: string; gp_letter: string; patient: string };
  hashes?: Record<string, string>;
  parseOk?: boolean;
  inputSha256?: string;
}

const TABS = [
  { key: 'summary',   label: 'Discharge summary' },
  { key: 'gp_letter', label: 'GP letter' },
  { key: 'patient',   label: 'Patient version' },
] as const;
type TabKey = (typeof TABS)[number]['key'];

/**
 * Tabbed view of the three drafts plus a small "evidence" footer showing the
 * input + per-output SHA-256 hashes, which the user can paste into the audit
 * log to confirm the row they're looking at matches the draft they have on
 * screen. Mirrors the slice-1 README's "what we log" claim, made concrete.
 */
export function OutputTabs({ outputs, hashes, parseOk, inputSha256 }: Props) {
  const [active, setActive] = useState<TabKey>('summary');
  return (
    <div className="output">
      <div className="tabs">
        {TABS.map((t) => (
          <button
            key={t.key}
            className={`tab ${active === t.key ? 'tab-active' : ''}`}
            onClick={() => setActive(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <pre className="output-body">{outputs[active]}</pre>
      <div className="evidence">
        {parseOk === false && (
          <p className="warn">
            ⚠ Splitter could not cleanly separate PART A/B/C — the full model
            output is shown under "Discharge summary". Audit row has parse_ok=false.
          </p>
        )}
        {inputSha256 && (
          <p className="muted">
            input sha256: <code>{inputSha256}</code>
          </p>
        )}
        {hashes && Object.keys(hashes).length > 0 && (
          <p className="muted">
            output sha256: {' '}
            {Object.entries(hashes).map(([k, v]) => (
              <span key={k} style={{ marginRight: '0.75em' }}>
                {k}=<code>{v.slice(0, 12)}…</code>
              </span>
            ))}
          </p>
        )}
      </div>
    </div>
  );
}
