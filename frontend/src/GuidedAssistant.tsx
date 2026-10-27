import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import type { AccountCandidate, LoanInspection, Session } from './types';
import './GuidedAssistant.css';
import InputReadiness from './InputReadiness';

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
      <InputReadiness items={inspection.missing_inputs} rules={inspection.rules} />
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
  const [aiConfirmation, setAIConfirmation] = useState<string | null>(null);
  const [aiError, setAIError] = useState('');
  const [aiPending, setAIPending] = useState(false);
  const attemptedReviews = useRef(new Set<string>());
  const aiRequestRunning = useRef(false);
  const aiResult = useRef<HTMLDivElement>(null);
  const loanSelector = useRef<HTMLSelectElement>(null);
  const preparedReview = useRef<{ sessionId: string; reportId: string | null } | null>(null);
  const [accountCandidates, setAccountCandidates] = useState<AccountCandidate[]>([]);
  const [accountTotal, setAccountTotal] = useState(0);
  const [accountSearch, setAccountSearch] = useState('');
  const [accountLoading, setAccountLoading] = useState(false);
  const resultPanel = useRef<HTMLDivElement>(null);
  const confirmationPanel = useRef<HTMLDivElement>(null);
  const loans = session.report?.underwriting?.loan_results ?? [];
  const reviewScope = `${session.id}:${session.report?.id ?? ''}`;
  const selectedLoan = loans.find((loan) => loan.loan_id === selected) ?? loans[0];
  const latestAIReview = [...(session.loan_ai_reviews ?? [])].reverse().find((item) =>
    item.report_id === session.report?.id && item.loan_id === selectedLoan?.loan_id);
  const preflight = session.preflight;
  const preflightAttributes = preflight?.attributes ?? [];
  const missingQueryRoles = preflight?.missing_query_roles ?? [];
  const duplicateQueryRoles = preflight?.duplicate_query_roles ?? [];
  const priorityIssues = preflight?.priority_issues ?? [];
  const unmappedAttributes = preflight?.unmapped_attributes ?? [];
  const unavailableInputs = preflight?.unavailable_inputs ?? [];
  const mappingWarnings = preflight?.mapping_warnings ?? [];
  const requiresAccountSelection = !!session.policy_rules?.length || preflight?.recognized_mtb === true
    || session.documents.some((document) => /^MTB[ _-]+Query[ _-]+(1|2|3|4)(?=$|[ _().-]).*\.xlsx$/i.test(document.name));
  const requiresWorkbookValidation = session.documents.some((document) => document.extension === '.xlsx') && !preflight;
  const queryFileSetValid = preflight?.query_file_set_valid ?? Boolean(
    preflight?.recognized_mtb && preflight.document_count === 3
    && [1, 2, 4].every((role) => preflight.query_roles?.includes(role)
      && !missingQueryRoles.includes(role) && !duplicateQueryRoles.includes(role)),
  );
  const latestInspection = [...session.messages].reverse().find((item) =>
    item.inspection?.loan_id === selectedLoan?.loan_id && item.inspection?.report_id === session.report?.id);
  useEffect(() => { setConfirmation(null); }, [session.id, session.updated_at]);
  useEffect(() => { attemptedReviews.current.clear(); }, [session.id, session.report?.id]);
  useEffect(() => { setAIConfirmation(null); setAIError(''); }, [session.id, session.updated_at, selectedLoan?.loan_id]);
  useEffect(() => {
    if (latestAIReview) {
      aiResult.current?.focus({ preventScroll: true });
      aiResult.current?.scrollIntoView?.({ behavior: 'smooth', block: 'center' });
    }
  }, [latestAIReview?.id]);
  useEffect(() => {
    if (confirmation) {
      confirmationPanel.current?.focus({ preventScroll: true });
      confirmationPanel.current?.scrollIntoView?.({ behavior: 'smooth', block: 'center' });
    }
  }, [confirmation]);
  useEffect(() => { setSelected(''); setNote(''); setFeedback(''); }, [session.id, session.report?.id]);
  useEffect(() => {
    const prepared = preparedReview.current;
    if (!prepared) return;
    if (prepared.sessionId !== session.id || session.status === 'failed') {
      preparedReview.current = null;
      return;
    }
    if (disabled || session.status !== 'completed' || !session.report || session.report.id === prepared.reportId) return;
    preparedReview.current = null;
    if (loans.length === 1) {
      setSelected(loans[0].loan_id);
      requestLoanReview(loans[0].loan_id, session.updated_at);
    } else if (loans.length > 1) {
      loanSelector.current?.focus();
      loanSelector.current?.scrollIntoView?.({ behavior: 'smooth', block: 'center' });
    }
  }, [session.id, session.status, session.report?.id, disabled]);
  useEffect(() => {
    let active = true;
    if (preflight?.recognized_mtb) {
      setAccountLoading(true);
      const timer = setTimeout(() => {
        void api.accounts(session.id, accountSearch).then((page) => {
          if (active) {
            setAccountCandidates(page.results);
            setAccountTotal(page.total);
            if (page.refreshed_preflight) {
              onSession({ ...session, preflight: page.refreshed_preflight });
            }
          }
        }).catch((cause) => {
          if (active) setFeedback(cause instanceof Error ? cause.message : 'Could not load detected borrower accounts.');
        }).finally(() => { if (active) setAccountLoading(false); });
      }, accountSearch.trim() ? 250 : 0);
      return () => { active = false; clearTimeout(timer); };
    }
    setAccountLoading(false);
    return () => { active = false; };
  }, [session.id, preflight?.input_fingerprint, preflight?.recognized_mtb, accountSearch]);
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

  async function loadAccounts(offset = 0) {
    setAccountLoading(true);
    try {
      const page = await api.accounts(session.id, accountSearch, offset);
      setAccountCandidates((current) => offset ? [...current, ...page.results] : page.results);
      setAccountTotal(page.total);
    } catch (cause) {
      setFeedback(cause instanceof Error ? cause.message : 'Could not search detected borrower accounts.');
    } finally {
      setAccountLoading(false);
    }
  }

  function requestLoanReview(loanId: string, revision: string) {
    if (!session.report || disabled || aiRequestRunning.current) return;
    const reportId = session.report.id;
    const key = `${session.id}:${reportId}:${loanId}`;
    if (attemptedReviews.current.has(key) || session.loan_ai_reviews?.some((item) => item.report_id === reportId && item.loan_id === loanId)) return;
    aiRequestRunning.current = true;
    attemptedReviews.current.add(key);
    setAIPending(true); setAIConfirmation(null); setAIError('');
    void run('Reviewing selected loan with Claude', async () => {
      try { onSession(await api.loanReview(session.id, loanId, reportId, revision)); }
      catch (cause) { setAIError(cause instanceof Error ? cause.message : 'Claude review failed. Your underwriting report is unchanged.'); }
      finally { aiRequestRunning.current = false; setAIPending(false); }
    });
  }

  const prepareReviewAction = !session.report && <div className="guided-actions" aria-label="Next step after account selection">
    <button type="button" className="button primary" disabled={disabled || !session.documents.length || requiresWorkbookValidation || accountLoading || (requiresAccountSelection && (!queryFileSetValid || ((accountTotal || preflight?.account_count || 0) > 0 && !session.selected_account_id)))} onClick={() => setConfirmation(session.updated_at)}>Prepare review</button>
  </div>;

  return <section className="guided-assistant" aria-label="Guided assistant actions">
    <span className="eyebrow">YOUR NEXT SAFE STEP</span>
    <h3>{session.status === 'processing' ? 'Your review is running' : session.report ? 'Let’s explore the evidence' : 'Let’s validate before reviewing'}</h3>
    <ol className="workflow-steps" aria-label="Review steps">
      <li><strong>1. Upload files</strong><span>Files validate automatically after upload. No provider call.</span></li>
      <li><strong>2. Choose account/client</strong><span>Select a detected borrower, or continue with diagnostics if none can be mapped.</span></li>
      <li><strong>{loans.length ? '3. Select a loan' : '3. Prepare review'}</strong><span>{loans.length ? 'Selecting a loan starts its Claude review; saved reviews are reused.' : 'Review the charge disclosure, then confirm initial report processing.'}</span></li>
    </ol>
    <p>{session.status === 'processing' ? 'Progress updates automatically. No duplicate review will be started.' : 'File validation and evidence lookups are local. Selecting a loan for Claude review sends normalized context to Anthropic and may incur charges. Notes cannot change verified evidence or clear policy conflicts.'}</p>
    {session.status === 'processing' && <ol>{session.steps.map((step) => <li key={step.agent}>{step.agent}: {step.status} — {step.detail}</li>)}</ol>}
    <details className="secondary-actions"><summary>File tools and report downloads</summary><div className="guided-actions">
      <button type="button" className="button secondary" disabled={disabled || !session.documents.length} onClick={() => void run('Validating files locally', async () => onSession(await api.preflight(session.id)))}>Validate files locally</button>
      <button type="button" className="button secondary" disabled={disabled} onClick={onUpload}>Add corrected source files</button>
      {session.report && <>
        <button type="button" className="button secondary" disabled={disabled} onClick={onReport}>Inspect full report</button>
        <button type="button" className="button secondary" disabled={disabled} onClick={onDownload}>Save HTML report</button>
      </>}
    </div></details>
    {!preflight && prepareReviewAction}
    {preflight && <div className="guided-preflight" role="status">
      <h4>Local validation summary</h4>
      <p>{preflight.document_count ?? session.documents.length} source file · {preflight.loan_count ?? 0} explicit loans · {preflight.issue_count ?? 0} structural issues.</p>
      <p>{preflight.recognized_mtb ? 'MTB source roles detected. Query 1, Query 2, and Query 4 link through matching Account ID columns; header spellings may vary. No row-position or name joins are used.' : 'Generic document — extracted attributes are available; no underwriting relationship validation claimed.'}</p>
      <p>{preflight.attribute_count ?? preflightAttributes.length} distinct column attributes extracted{preflightAttributes.length < (preflight.attribute_count ?? preflightAttributes.length) ? ` · showing first ${preflightAttributes.length}` : ''}.</p>
      {!!preflightAttributes.length && <details><summary>View extracted attributes</summary><ul>{preflightAttributes.map((attribute) => <li key={attribute}>{attribute}</li>)}</ul></details>}
      <p>{preflight.unmapped_attribute_count ?? unmappedAttributes.length} source columns currently unmapped; unmapped does not mean the source cell was discarded.</p>
      {queryFileSetValid && <p>Missing field mappings do not block processing. Continue with available evidence; affected checks remain UNKNOWN. No relationships or values are inferred.</p>}
      {!!unmappedAttributes.length && <details><summary>View unmapped source columns</summary><ul>{unmappedAttributes.map((attribute) => <li key={attribute}>{attribute}</li>)}</ul></details>}
      {!!unavailableInputs.length && <details><summary>Required canonical inputs not present in these files</summary><ul>{unavailableInputs.map((input) => <li key={input}>{input}</li>)}</ul></details>}
      {!!mappingWarnings.length && <details><summary>Relationships not resolvable from supplied columns</summary><ul>{mappingWarnings.map((warning) => <li key={warning}>{warning}</li>)}</ul></details>}
      {preflight.recognized_mtb && <>
        {!!missingQueryRoles.length && <p>Missing source roles: Query {missingQueryRoles.join(', Query ')}.</p>}
        {!!duplicateQueryRoles.length && <p>Duplicate source roles: Query {duplicateQueryRoles.join(', Query ')}; keep one workbook per role.</p>}
        <p>{preflight.account_count ?? 0} borrower accounts detected across {preflight.loan_count ?? 0} explicit loans. Local validation indexes relationships; it does not evaluate every client's credit.</p>
        <label>Search accounts or clients<input aria-label="Search accounts or clients" value={accountSearch} onChange={(event) => setAccountSearch(event.target.value)} placeholder="Account name or ID" /></label>
        <div className="guided-actions">
          <button type="button" className="button secondary" disabled={disabled || accountLoading} onClick={() => void loadAccounts(0)}>Search accounts</button>
          <label>Choose account/client<select aria-label="Choose account/client" value={session.selected_account_id ?? ''} disabled={disabled || accountLoading || !(accountTotal || (preflight.account_count ?? 0))}
            onChange={(event) => {
              const accountId = event.target.value;
              if (accountId) void run('Selecting borrower account', async () => onSession(await api.selectAccount(session.id, accountId)));
            }}>
            <option value="">Select a borrower account/client</option>
            {session.selected_account_id && !accountCandidates.some((item) => item.account_id === session.selected_account_id)
              && <option value={session.selected_account_id}>{session.selected_account_id} · selected</option>}
            {accountCandidates.map((item) => <option key={item.account_id} value={item.account_id}>
              {item.name ? `${item.name} · ${item.account_id}` : item.account_id} · {item.loan_count} {item.loan_count === 1 ? 'loan' : 'loans'}
              {accountSearch.trim() && item.loan_ids.some((loanId) => loanId.toLowerCase().startsWith(accountSearch.trim().toLowerCase()))
                ? ` · matched loan ${item.loan_ids.find((loanId) => loanId.toLowerCase().startsWith(accountSearch.trim().toLowerCase()))}` : ''}
            </option>)}
          </select></label>
          {accountSearch.trim() && accountCandidates.length > 0 && <ul aria-label="Matching borrower accounts">
            {accountCandidates.map((item) => <li key={item.account_id}>
              <button type="button" className="text-button" disabled={disabled || accountLoading}
                onClick={() => void run('Selecting borrower account', async () => onSession(await api.selectAccount(session.id, item.account_id)))}>
                Select {item.name ? `${item.name} · ` : ''}{item.account_id}
              </button>
              {item.loan_ids.length ? ` — linked loan${item.loan_ids.length === 1 ? '' : 's'}: ${item.loan_ids.join(', ')}` : ''}
            </li>)}
          </ul>}
          {accountCandidates.length < accountTotal && <button type="button" className="button secondary" disabled={disabled || accountLoading} onClick={() => void loadAccounts(accountCandidates.length)}>Load more accounts</button>}
        </div>
        {requiresAccountSelection && !session.selected_account_id && <small>{!queryFileSetValid ? 'Upload one workbook each for Query 1, Query 2, and Query 4 before preparing the review.' : accountLoading ? 'Loading borrower accounts…' : accountTotal || (preflight.account_count ?? 0) ? 'Select an account/client before preparing the review.' : 'No borrower accounts could be mapped. You can still use Prepare review for a diagnostic-only report without account selection; no loan records or relationships will be invented.'}</small>}
      </>}
      {prepareReviewAction}
      <h5>Priority blockers · first {priorityIssues.length} of {preflight.issue_count ?? priorityIssues.length}</h5>
      <ul>{priorityIssues.map((issue, i) => <li key={i}>{issue}</li>)}</ul>
      <p>{preflight.suggested_next_step ?? 'Review the extracted attributes and source relationships before processing.'}</p><p>{preflight.comparison ?? 'No previous validation comparison is available.'}</p>
      <small>All source-row attributes are extracted. Cross-file relationships use matching explicit Account ID values only; local validation is not a lending decision.</small>
    </div>}
    {confirmation && <div ref={confirmationPanel} tabIndex={-1} className="guided-confirmation" role="group" aria-label="Confirm external processing">
      <h4>Start {session.mode ?? 'TEST'} review?</h4>
      <p>Processing sends bounded session/document context to Anthropic for commentary and may incur charges. It replaces the current report. If the result contains one loan, this confirmation also starts one additional selected-loan Claude review automatically, which may incur an additional charge. For multiple loans, choose the intended loan when processing finishes. Missing inputs and policy conflicts remain review items; no lending authorization is issued.</p>
      <button type="button" className="button primary" disabled={disabled} onClick={() => void run('Starting confirmed review', async () => {
        preparedReview.current = { sessionId: session.id, reportId: session.report?.id ?? null };
        try { onSession(await api.guidedProcess(session.id, confirmation)); setConfirmation(null); }
        catch (cause) { preparedReview.current = null; throw cause; }
      })}>Confirm and process</button>
      <button type="button" className="button secondary" disabled={disabled} onClick={() => setConfirmation(null)}>Cancel review</button>
    </div>}
    {!!loans.length && <div className="guided-loan">
      <p id="loan-review-disclosure">Selecting a loan starts its Claude review automatically and confirms sending bounded normalized evidence to Anthropic; each new review may incur charges. Raw workbook rows are not sent. Saved reviews are reused. Opening this screen does not start a request, and failures are not retried automatically.</p>
      <label>Loan to explore<select ref={loanSelector} aria-label="Loan to explore" value={selected || ''} disabled={disabled || aiPending} onChange={(event) => {
        const loanId = event.target.value;
        setSelected(loanId);
        if (loanId) requestLoanReview(loanId, session.updated_at);
      }} aria-describedby="loan-review-disclosure"><option value="">Select a loan to review with Claude</option>{loans.map((loan) => <option key={loan.loan_id} value={loan.loan_id}>{loan.loan_id}</option>)}</select></label>
      <details className="secondary-actions"><summary>Evidence tools and follow-up questions</summary><div className="guided-actions">
        <button type="button" className="button secondary" disabled={disabled} onClick={() => inspect('blockers')}>Why is this loan held?</button>
        <button type="button" className="button secondary" disabled={disabled} onClick={() => inspect('rules')}>Inspect loan rules</button>
        <button type="button" className="button secondary" disabled={disabled} onClick={() => inspect('evidence')}>Show source cells</button>
        <button type="button" className="button secondary" disabled={disabled || !selectedLoan} onClick={() => {
          if (selectedLoan) {
            onAsk(selectedLoan.loan_id, 'Explain the priority blockers for this selected loan, citing supplied rules. What source evidence is missing?');
            setFeedback('Question drafted in Message Agent below. Review it and press Send to ask Claude; it has not been sent.');
          }
        }}>Draft a question for Claude</button>
      </div></details>
      {aiConfirmation && <div className="guided-confirmation" role="group" aria-label="Confirm selected-loan Claude review">
        <h4>Claude review · {selectedLoan?.loan_id}</h4>
        <p>This sends bounded normalized evidence, rule logic and evidence IDs for this loan to Anthropic and may incur charges. Raw Query 1/2/4 rows, unmapped attributes and chat history are not sent. Claude’s rationale is separate commentary; the saved underwriting report and manual-review requirement do not change.</p>
        <button type="button" className="button primary" disabled={disabled || aiPending} onClick={() => {
          if (!selectedLoan || !session.report) return;
          attemptedReviews.current.delete(`${reviewScope}:${selectedLoan.loan_id}`);
          requestLoanReview(selectedLoan.loan_id, aiConfirmation);
        }}>Confirm Claude review</button>
        <button type="button" className="button secondary" disabled={disabled} onClick={() => setAIConfirmation(null)}>Cancel Claude review</button>
      </div>}
      {aiPending && <p role="status">Claude is reviewing the selected loan. No new recommendation or decision has been issued.</p>}
      {aiError && <div className="inline-error" role="alert"><p>{aiError} Your saved report is unchanged. Another request may incur charges.</p><button type="button" className="button secondary" disabled={disabled || aiPending} onClick={() => setAIConfirmation(session.updated_at)}>Retry Claude review</button></div>}
      {latestAIReview && <div ref={aiResult} tabIndex={-1} aria-label="Selected-loan Claude review result">
        <section className="claude-explanation" aria-label="Selected-loan Claude rationale">
          <h4>Claude rationale · {latestAIReview.loan_id}</h4>
          <p>MANUAL_REVIEW · No approval or denial · Authorized human review required.</p>
          <p className="claude-model">Model: {latestAIReview.model}</p>
          <p className="claude-analysis">{latestAIReview.analysis.explanation}</p>
          {!!latestAIReview.analysis.check_reconciliation?.length && <section aria-label="Claude reconciliation of unresolved checks">
            <h5>Claude’s assessment of unresolved checks</h5>
            <p>These are AI explanations of the supplied evidence—not rule clearances or new verified facts.</p>
            {latestAIReview.analysis.check_reconciliation.map((check) => <article className="policy-result" key={check.rule_id}>
              <h6>{check.rule_id}</h6>
              <p>{({ MAPPED_EVIDENCE_BLOCKED: 'Mapped evidence found—use is blocked', MISSING_OR_UNVERIFIED_INPUT: 'Input missing or not verified', CONFLICTING_EVIDENCE: 'Conflicting evidence', POLICY_UNRESOLVED: 'Policy clarification required' })[check.evidence_state]}</p>
              <p>{check.rationale}</p><p><strong>Next step:</strong> {check.next_step}</p>
              {check.evidence_refs.map((id) => {
                const source = selectedLoan?.source_evidence.find((item) => item.id === id);
                return <p key={id}>{id}{source ? ` — ${source.workbook} / ${source.sheet} / row ${source.row} / ${source.field}` : ' — source not available in this view'}</p>;
              })}
            </article>)}
          </section>}
          {([['Supported factors', latestAIReview.analysis.positive_factors], ['Risks and contradictions', latestAIReview.analysis.risk_factors], ['Next evidence needed', latestAIReview.analysis.missing_information]] as const).map(([heading, items]) => <div key={heading}><h5>{heading}</h5><ul>{items.map((item, i) => <li key={i}>{item}</li>)}</ul></div>)}
          <p>Failed-rule citations: {latestAIReview.analysis.triggered_rules.join(', ') || 'None supplied'}</p>
          <details><summary>Cited source evidence</summary>
            {latestAIReview.analysis.evidence_refs.map((id) => {
              const evidence = selectedLoan?.source_evidence.find((item) => item.id === id);
              return <p key={id}>{id}{evidence ? ` — ${evidence.workbook} / ${evidence.sheet} / row ${evidence.row} / ${evidence.field}` : ' — unavailable in this view'}</p>;
            })}
          </details>
          <p>{latestAIReview.context_note}</p>
          <small>Saved against report {latestAIReview.report_id}. AI commentary is not new verified evidence.</small>
        </section>
      </div>}
      {feedback && <p role="status">{feedback}</p>}
      {latestInspection?.inspection && <div ref={resultPanel} tabIndex={-1} aria-label="Latest loan lookup result">
        <InspectionCard key={latestInspection.id} inspection={latestInspection.inspection} />
      </div>}
      <p>Claude questions require Send and billable external processing. Local evidence lookups do not.</p>
      <details className="secondary-actions"><summary>Review notes and acknowledgments</summary>
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
      </details>
    </div>}
  </section>;
}