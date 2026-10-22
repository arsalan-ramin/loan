import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import type { LoanInspection, Session } from './types';
import './GuidedAssistant.css';

interface Props {
  session: Session;
  disabled: boolean;
  run: (label: string, work: () => Promise<void>) => Promise<void>;
  onSession: (session: Session) => void;
  onUpload: () => void;
  onReport: () => void;
  onDownload: () => void;
  onAsk: (loanId: string, question: string) => void;
}

export function InspectionCard({ inspection }: { inspection: LoanInspection }) {
  return <section className="guided-inspection" aria-label={`Saved evidence for loan ${inspection.loan_id}`}>
    <h4>Loan {inspection.loan_id} · {inspection.topic}</h4>
    <p>Saved report {inspection.report_id}. Historical snapshot; reprocessing can change the current report.</p>
    <p>{inspection.recommendation_path} · {inspection.final_action} · authorized human review required.</p>
    {inspection.topic === 'blockers' && <>
      <h5>Missing inputs</h5><ul>{inspection.missing_inputs.map((item, i) => <li key={i}>{item}</li>)}</ul>
      <h5>Conflicts</h5><ul>{inspection.conflicts.map((item, i) => <li key={i}>{item}</li>)}</ul>
    </>}
    {inspection.topic !== 'evidence' && <><h5>Priority rules · showing {inspection.rules.length} of {inspection.total_rules}</h5>
      {inspection.rules.map((rule) => <div key={rule.rule_id}><strong>{rule.rule_id} · {rule.status}</strong><p>{rule.detail}</p><p>Evidence: {rule.evidence_refs.join(', ') || 'None supplied'}</p></div>)}
    </>}
    <details open={inspection.topic === 'evidence'}><summary>Source cells · showing {inspection.evidence.length} of {inspection.total_evidence}</summary>
      {inspection.evidence.map((item) => <div key={item.id}><strong>{item.id}</strong>
        <p>{item.workbook} / {item.sheet} / row {item.row} / {item.field}</p>
        <pre>{item.value === null ? 'Unknown' : String(item.value)}</pre>
      </div>)}
    </details>
    <p>Bounded local lookup, not an LLM response. Open the full report for all rules and evidence.</p>
  </section>;
}

export default function GuidedAssistant({ session, disabled, run, onSession, onUpload, onReport, onDownload, onAsk }: Props) {
  const [selected, setSelected] = useState('');
  const [confirmation, setConfirmation] = useState<string | null>(null);
  const [note, setNote] = useState('');
  const [kind, setKind] = useState<'note' | 'acknowledgment'>('note');
  const [feedback, setFeedback] = useState('');
  const resultPanel = useRef<HTMLDivElement>(null);
  const loans = session.report?.underwriting?.loan_results ?? [];
  const selectedLoan = loans.find((loan) => loan.loan_id === selected) ?? loans[0];
  const preflight = session.preflight;
  const latestInspection = [...session.messages].reverse().find((item) =>
    item.inspection?.loan_id === selectedLoan?.loan_id && item.inspection?.report_id === session.report?.id);
  useEffect(() => { setConfirmation(null); }, [session.id, session.updated_at]);
  useEffect(() => { setSelected(''); setNote(''); setFeedback(''); }, [session.id, session.report?.id]);
  useEffect(() => {
    if (latestInspection) {
      resultPanel.current?.focus({ preventScroll: true });
      resultPanel.current?.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' });
    }
  }, [latestInspection?.id]);

  function inspect(topic: 'blockers' | 'rules' | 'evidence') {
    if (!selectedLoan) return;
    setFeedback('Looking up saved loan evidence…');
    void run('Inspecting saved evidence', async () => {
      try {
        onSession(await api.inspectLoan(session.id, selectedLoan.loan_id, topic));
        setFeedback('Lookup complete. The result is directly below these buttons.');
      } catch (cause) {
        setFeedback(cause instanceof Error ? cause.message : 'Lookup failed. Try again.');
        throw cause;
      }
    });
  }

  return <section className="guided-assistant" aria-label="Guided assistant actions">
    <span className="eyebrow">YOUR NEXT SAFE STEP</span>
    <h3>{session.status === 'processing' ? 'Your review is running' : session.report ? 'Let’s explore the evidence' : 'Let’s validate before reviewing'}</h3>
    <p>{session.status === 'processing' ? 'Progress updates automatically. No duplicate review will be started.' : 'Local actions below do not call Anthropic. Notes and chat assertions cannot change verified evidence or clear policy conflicts.'}</p>
    {session.status === 'processing' && <ol>{session.steps.map((step) => <li key={step.agent}>{step.agent}: {step.status} — {step.detail}</li>)}</ol>}
    <div className="guided-actions">
      <button type="button" className="button secondary" disabled={disabled || !session.documents.length} onClick={() => void run('Validating files locally', async () => onSession(await api.preflight(session.id)))}>Validate files locally</button>
      <button type="button" className="button secondary" disabled={disabled} onClick={onUpload}>Add corrected source files</button>
      <button type="button" className="button primary" disabled={disabled || !session.documents.length} onClick={() => setConfirmation(session.updated_at)}>Prepare review</button>
      {session.report && <>
        <button type="button" className="button secondary" disabled={disabled} onClick={onReport}>Inspect full report</button>
        <button type="button" className="button secondary" disabled={disabled} onClick={onDownload}>Save HTML report</button>
      </>}
    </div>
    {preflight && <div className="guided-preflight" role="status">
      <h4>Local validation summary</h4>
      <p>{preflight.document_count} files · {preflight.loan_count} explicit loans · {preflight.issue_count} diagnostic issues.</p>
      <p>{preflight.recognized_mtb ? `Recognized Query roles: ${preflight.query_roles.join(', ')}` : 'Generic documents — no underwriting relationship validation claimed.'}</p>
      {!!preflight.missing_query_roles.length && <p>Missing Query roles: {preflight.missing_query_roles.join(', ')}</p>}
      <h5>Priority blockers · first {preflight.priority_issues.length} of {preflight.issue_count}</h5>
      <ul>{preflight.priority_issues.map((issue, i) => <li key={i}>{issue}</li>)}</ul>
      <p>{preflight.suggested_next_step}</p><p>{preflight.comparison}</p>
      <small>Local validation is not a lending decision or a clean credit report. Source corrections require replacement uploads; remove superseded files to avoid conflicting duplicates.</small>
    </div>}
    {confirmation && <div className="guided-confirmation" role="group" aria-label="Confirm external processing">
      <h4>Start {session.mode ?? 'PRODUCTION'} review?</h4>
      <p>Processing sends bounded session/document context to Anthropic for commentary and may incur charges. It replaces the current report. Missing inputs and policy conflicts will remain review items; no lending authorization is issued.</p>
      <button type="button" className="button primary" disabled={disabled} onClick={() => void run('Starting confirmed review', async () => { onSession(await api.guidedProcess(session.id, confirmation)); setConfirmation(null); })}>Confirm and process</button>
      <button type="button" className="button secondary" disabled={disabled} onClick={() => setConfirmation(null)}>Cancel review</button>
    </div>}
    {!!loans.length && <div className="guided-loan">
      <label>Loan to explore<select aria-label="Loan to explore" value={selectedLoan?.loan_id ?? ''} disabled={disabled} onChange={(event) => setSelected(event.target.value)}>{loans.map((loan) => <option key={loan.loan_id} value={loan.loan_id}>{loan.loan_id}</option>)}</select></label>
      <div className="guided-actions">
        <button type="button" className="button secondary" disabled={disabled} onClick={() => inspect('blockers')}>Why is this loan held?</button>
        <button type="button" className="button secondary" disabled={disabled} onClick={() => inspect('rules')}>Inspect loan rules</button>
        <button type="button" className="button secondary" disabled={disabled} onClick={() => inspect('evidence')}>Show source cells</button>
        <button type="button" className="button secondary" disabled={disabled || !selectedLoan} onClick={() => {
          if (selectedLoan) {
            onAsk(selectedLoan.loan_id, 'Explain the priority blockers for this selected loan, citing supplied rules. What source evidence is missing?');
            setFeedback('Question drafted in Message Agent below. Review it and press Send to ask Claude; it has not been sent.');
          }
        }}>Draft a question for Claude</button>
      </div>
      {feedback && <p role="status">{feedback}</p>}
      {latestInspection?.inspection && <div ref={resultPanel} tabIndex={-1} aria-label="Latest loan lookup result">
        <InspectionCard key={latestInspection.id} inspection={latestInspection.inspection} />
      </div>}
      <p>Claude questions require Send and billable external processing. Local evidence lookups do not.</p>
      <p><strong>Want an answer?</strong> Use Message Agent below and press Send. The review note field only saves a note; it does not ask the agent a question.</p>
      <label>Review note (unverified assertion)<textarea aria-label="Review note" maxLength={2000} value={note} disabled={disabled} onChange={(event) => setNote(event.target.value)} /></label>
      <label>Record as<select aria-label="Review record kind" value={kind} disabled={disabled} onChange={(event) => setKind(event.target.value as typeof kind)}><option value="note">Note</option><option value="acknowledgment">Acknowledgment — not clearance</option></select></label>
      <button type="button" className="button secondary" disabled={disabled || !note.trim() || !selectedLoan} onClick={() => void run('Recording review note', async () => {
        if (selectedLoan && session.report) {
          onSession(await api.reviewNote(session.id, session.report.id, selectedLoan.loan_id, note, kind));
          setNote(''); setFeedback('Note saved only — no question was sent and no rule outcome or authorization changed.');
        }
      })}>Record review note</button>
      <ul>{session.review_notes?.filter((item) => item.report_id === session.report?.id && item.loan_id === selectedLoan?.loan_id).map((item) => <li key={item.id}>{item.kind}: {item.text} — unverified user assertion, not evidence or approval</li>)}</ul>
    </div>}
  </section>;
}