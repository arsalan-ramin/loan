import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import type { LoanCandidate, LoanInspection, Session } from './types';
import './GuidedAssistant.css';
import InputReadiness from './InputReadiness';
import ClaudeExplanationText from './ClaudeExplanationText';

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
  const [recordChoiceLoan, setRecordChoiceLoan] = useState<string | null>(null);
  const [preferredRecordRef, setPreferredRecordRef] = useState('');
  const attemptedReviews = useRef(new Set<string>());
  const aiRequestRunning = useRef(false);
  const loanSelector = useRef<HTMLSelectElement>(null);
  const sourceLoanSelector = useRef<HTMLSelectElement>(null);
  const [focusLoanSession, setFocusLoanSession] = useState<string | null>(null);
  const preparedReview = useRef<{ sessionId: string; reportId: string | null } | null>(null);
  const [loanCandidates, setLoanCandidates] = useState<LoanCandidate[]>([]);
  const [loanTotal, setLoanTotal] = useState(0);
  const [loanSearch, setLoanSearch] = useState('');
  const [loanNumber, setLoanNumber] = useState('');
  const loanNumberInput = useRef<HTMLInputElement>(null);
  const [loanLoading, setLoanLoading] = useState(false);
  const confirmationPanel = useRef<HTMLDivElement>(null);
  const loans = session.report?.underwriting?.loan_results ?? [];
  const reviewScope = `${session.id}:${session.report?.id ?? ''}`;
  const selectedLoan = loans.find((loan) => loan.loan_id === (selected || session.selected_loan_id)) ?? loans[0];
  const demo = selectedLoan?.normalized_data.demo_decision as Record<string, unknown> | undefined;
  const showDemo = session.report?.underwriting?.mode === 'TEST' && selectedLoan?.mode === 'TEST'
    && demo?.version === 'screenshot-test-demo-v1' && demo.simulation_only === true
    && demo.authorization === 'NOT_ISSUED' && ['Approve', 'Decline', 'Refer'].includes(String(demo.label));
  const missingDemoSnapshot = session.report?.underwriting?.mode === 'TEST' && !!selectedLoan
    && !showDemo;
  const recordCandidates = selectedLoan?.source_evidence.filter((item) =>
    item.record_id && /(?:Credit_Score__c|personal_fico|^fico$)/i.test(item.field)) ?? [];
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
  const hasWorkbooks = session.documents.some((document) => document.extension === '.xlsx');
  const requiresLoanSelection = hasWorkbooks || !!session.policy_rules?.length || preflight?.recognized_mtb === true
    || session.documents.some((document) => /^MTB[ _-]+Query[ _-]+(1|2|3|4)(?=$|[ _().-]).*\.xlsx$/i.test(document.name));
  const requiresWorkbookValidation = hasWorkbooks
    && (!session.selected_loan_id || preflight?.scoped_loan_id !== session.selected_loan_id);
  const queryFileSetValid = preflight?.query_file_set_valid ?? Boolean(
    preflight?.recognized_mtb && preflight.document_count === 3
    && [1, 2, 4].every((role) => preflight.query_roles?.includes(role)
      && !missingQueryRoles.includes(role) && !duplicateQueryRoles.includes(role)),
  );
  const latestInspection = [...session.messages].reverse().find((item) =>
    item.inspection?.loan_id === selectedLoan?.loan_id && item.inspection?.report_id === session.report?.id);
  useEffect(() => { setConfirmation(null); }, [session.id, session.updated_at]);
  useEffect(() => { attemptedReviews.current.clear(); setRecordChoiceLoan(null); setPreferredRecordRef(''); }, [session.id, session.report?.id]);
  useEffect(() => { setAIConfirmation(null); setAIError(''); }, [session.id, session.updated_at, selectedLoan?.loan_id]);
  useEffect(() => {
    if (confirmation) {
      confirmationPanel.current?.focus({ preventScroll: true });
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
      beginLoanReview(loans[0].loan_id);
    }
  }, [session.id, session.status, session.report?.id, disabled]);
  useEffect(() => {
    let active = true;
    if (preflight?.recognized_mtb && !preflight.requires_loan_number && !preflight.scoped_loan_id) {
      setLoanLoading(true);
      const timer = setTimeout(() => {
        void api.loans(session.id, loanSearch).then((page) => {
          if (active) {
            setLoanCandidates(page.results);
            setLoanTotal(page.total);
          }
        }).catch((cause) => {
          if (active) setFeedback(cause instanceof Error ? cause.message : 'Could not load explicit loan numbers.');
        }).finally(() => { if (active) setLoanLoading(false); });
      }, loanSearch.trim() ? 250 : 0);
      return () => { active = false; clearTimeout(timer); };
    }
    setLoanLoading(false);
    return () => { active = false; };
  }, [session.id, preflight?.input_fingerprint, preflight?.recognized_mtb, preflight?.requires_loan_number, preflight?.scoped_loan_id, loanSearch]);
  useEffect(() => { setLoanNumber(''); }, [session.id]);
  useEffect(() => {
    if (!focusLoanSession) return;
    if (focusLoanSession !== session.id) {
      setFocusLoanSession(null);
      return;
    }
    if (disabled || loanLoading || !preflight?.recognized_mtb) return;
    if (sourceLoanSelector.current && !sourceLoanSelector.current.disabled) {
      sourceLoanSelector.current.focus({ preventScroll: true });
      setFocusLoanSession(null);
    }
  }, [focusLoanSession, session.id, disabled, loanLoading, preflight, loanTotal]);

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

  async function loadLoans(offset = 0) {
    setLoanLoading(true);
    try {
      const page = await api.loans(session.id, loanSearch, offset);
      setLoanCandidates((current) => offset ? [...current, ...page.results] : page.results);
      setLoanTotal(page.total);
    } catch (cause) {
      setFeedback(cause instanceof Error ? cause.message : 'Could not search explicit loan numbers.');
    } finally {
      setLoanLoading(false);
    }
  }

  function beginLoanReview(loanId: string) {
    const loan = loans.find((item) => item.loan_id === loanId);
    const candidates = loan?.source_evidence.filter((item) => item.record_id && /(?:Credit_Score__c|personal_fico|^fico$)/i.test(item.field)) ?? [];
    if (new Set(candidates.map((item) => `${item.record_id}:${String(item.value)}`)).size > 1) {
      setPreferredRecordRef(''); setRecordChoiceLoan(loanId);
    } else {
      setRecordChoiceLoan(null); requestLoanReview(loanId, session.updated_at);
    }
  }

  function requestLoanReview(loanId: string, revision: string, preferredRef?: string) {
    if (!session.report || disabled || aiRequestRunning.current) return;
    const reportId = session.report.id;
    const key = `${session.id}:${reportId}:${loanId}`;
    if (attemptedReviews.current.has(key) || session.loan_ai_reviews?.some((item) => item.report_id === reportId && item.loan_id === loanId)) return;
    aiRequestRunning.current = true;
    attemptedReviews.current.add(key);
    setAIPending(true); setAIConfirmation(null); setAIError('');
    void run('Reviewing selected loan with Claude', async () => {
      try { onSession(await api.loanReview(session.id, loanId, reportId, revision, preferredRef)); }
      catch (cause) { setAIError(cause instanceof Error ? cause.message : 'Claude review failed. Your underwriting report is unchanged.'); }
      finally { aiRequestRunning.current = false; setAIPending(false); }
    });
  }

  const prepareReviewAction = !session.report && <div className="guided-actions" aria-label="Next step after loan selection">
    <button type="button" className="button primary" disabled={disabled || !session.documents.length || requiresWorkbookValidation || loanLoading || (requiresLoanSelection && (!queryFileSetValid || ((loanTotal || preflight?.loan_count || 0) > 0 && !session.selected_loan_id)))} onClick={() => setConfirmation(session.updated_at)}>Prepare review</button>
  </div>;

  return <section className="guided-assistant" aria-label="Guided assistant actions">
    {showDemo ? <section className="guided-demo-summary" aria-label={`TEST demo summary for ${selectedLoan!.loan_id}`}>
      <span className="demo-label">TEST demo • no lending action</span>
      <h3>{String(demo!.label)} · Loan {selectedLoan!.loan_id}</h3>
      {typeof demo!.rationale === 'string' && <p>{demo!.rationale}</p>}
      {Array.isArray(demo!.matched_reasons) && demo!.matched_reasons.length > 0 && <div>
        <h5>Matched reasons</h5>
        <ul>{demo!.matched_reasons.map((item, index) => {
          const reason = item as Record<string, unknown>;
          return <li key={index}>{String(reason.label ?? reason.criterion ?? 'Matched criterion')}</li>;
        })}</ul>
      </div>}
      <p className="muted">Final lending decisions require authorized human review.</p>
      <details><summary>Demo criteria and source evidence</summary><pre>{JSON.stringify(demo, null, 2)}</pre></details>
    </section> : <h3>{session.status === 'processing' ? 'Your review is running' : session.report ? 'Explore this loan' : 'Select a loan and prepare review'}</h3>}
    {session.report?.underwriting?.mode === 'PRODUCTION' && <p className="policy-disclaimer">PRODUCTION — manual review only. No automated lending decision.</p>}
    <details className="secondary-actions"><summary>Workflow and processing details</summary>
    <ol className="workflow-steps" aria-label="Review steps">
      <li><strong>1. Upload files</strong><span>Files are saved without loading workbook data. No provider call.</span></li>
      <li><strong>2. Enter loan number</strong><span>Only rows explicitly related to this loan are loaded and validated locally.</span></li>
      <li><strong>{loans.length ? '3. Select a loan' : '3. Prepare review'}</strong><span>{loans.length ? 'Selecting a loan starts its Claude review; saved reviews are reused.' : 'Review the charge disclosure, then confirm initial report processing.'}</span></li>
    </ol>
    <p>{session.status === 'processing' ? 'Progress updates automatically. No duplicate review will be started.' : 'File validation and evidence lookups are local. Selecting a loan for Claude review sends normalized context to Anthropic and may incur charges. Notes cannot change verified evidence or clear policy conflicts.'}</p>
    {session.status === 'processing' && <ol>{session.steps.map((step) => <li key={step.agent}>{step.agent}: {step.status} — {step.detail}</li>)}</ol>}
    </details>
    {session.status === 'completed' && session.report && session.documents.some((document) => document.extension === '.xlsx') && <div className="guided-actions">
      <button type="button" className="button primary" disabled={disabled || aiPending} onClick={() => {
        setLoanSearch('');
        if (hasWorkbooks) loanNumberInput.current?.focus();
        else if (preflight?.recognized_mtb) setFocusLoanSession(session.id);
        else void run('Validating files locally to choose another loan', async () => {
          const validated = await api.preflight(session.id);
          onSession(validated);
          if (validated.preflight?.recognized_mtb) setFocusLoanSession(session.id);
        });
      }}>Choose another loan</button>
      <small>Reuse these uploaded files. Search/select a loan locally; no provider call or charge occurs until you confirm processing.</small>
    </div>}
    <details className="secondary-actions"><summary>File tools and report downloads</summary><div className="guided-actions">
      <button type="button" className="button secondary" disabled={disabled || !session.documents.length} onClick={() => void run('Validating files locally', async () => onSession(await api.preflight(session.id)))}>Validate files locally</button>
      <button type="button" className="button secondary" disabled={disabled} onClick={onUpload}>Add corrected source files</button>
      {session.report && <>
        <button type="button" className="button secondary" disabled={disabled} onClick={onReport}>Inspect full report</button>
        <button type="button" className="button secondary" disabled={disabled} onClick={onDownload}>Save HTML report</button>
      </>}
    </div></details>
    {hasWorkbooks && <form className="guided-loan" aria-label="Load one loan" onSubmit={(event) => {
      event.preventDefault();
      const number = loanNumber.trim();
      if (!number || disabled) return;
      void run('Loading selected loan data locally', async () => onSession(await api.selectLoan(session.id, number)));
    }}>
      <p>What loan number would you like to review? Only data explicitly related to that loan will be loaded from the uploaded files.</p>
      <label>Loan number<input ref={loanNumberInput} aria-label="Loan number" value={loanNumber}
        onChange={(event) => setLoanNumber(event.target.value)} placeholder="Enter the exact loan number" disabled={disabled} maxLength={128} /></label>
      <button type="submit" className="button primary" disabled={disabled || !loanNumber.trim()}>Load loan data</button>
      {session.selected_loan_id && <p>Loaded loan {session.selected_loan_id}. Enter another number to switch loans without uploading again.</p>}
      {session.selected_loan_id && !session.selected_account_id && <p className="muted">Guarantor records retain their roles; missing borrower evidence remains UNKNOWN.</p>}
    </form>}
    {!preflight && prepareReviewAction}
    {preflight && <div className="guided-preflight" role="status">
      <h4>Local validation summary</h4>
      <p>{preflight.document_count ?? session.documents.length} source file · {preflight.loan_count ?? 0} explicit loans · {preflight.issue_count ?? 0} structural issues.</p>
      <details className="preflight-audit"><summary>Validation details, attributes and issues</summary>
      <p>{preflight.recognized_mtb ? 'MTB source roles detected. Query 1, Query 2, and Query 4 link through matching Account ID columns; header spellings may vary. No row-position or name joins are used.' : 'Generic document — extracted attributes are available; no underwriting relationship validation claimed.'}</p>
      <p>{preflight.attribute_count ?? preflightAttributes.length} distinct column attributes extracted{preflightAttributes.length < (preflight.attribute_count ?? preflightAttributes.length) ? ` · showing first ${preflightAttributes.length}` : ''}.</p>
      {!!preflightAttributes.length && <details><summary>View extracted attributes</summary><ul>{preflightAttributes.map((attribute) => <li key={attribute}>{attribute}</li>)}</ul></details>}
      <p>{preflight.unmapped_attribute_count ?? unmappedAttributes.length} source columns currently unmapped; unmapped does not mean the source cell was discarded.</p>
      {queryFileSetValid && <p>Missing field mappings do not block processing. Continue with available evidence; affected checks remain UNKNOWN. No relationships or values are inferred.</p>}
      {!!unmappedAttributes.length && <details><summary>View unmapped source columns</summary><ul>{unmappedAttributes.map((attribute) => <li key={attribute}>{attribute}</li>)}</ul></details>}
      {!!unavailableInputs.length && <details><summary>Required canonical inputs not present in these files</summary><ul>{unavailableInputs.map((input) => <li key={input}>{input}</li>)}</ul></details>}
      {!!mappingWarnings.length && <details><summary>Relationships not resolvable from supplied columns</summary><ul>{mappingWarnings.map((warning) => <li key={warning}>{warning}</li>)}</ul></details>}
        {!!missingQueryRoles.length && <p>Missing source roles: Query {missingQueryRoles.join(', Query ')}.</p>}
        {!!duplicateQueryRoles.length && <p>Duplicate source roles: Query {duplicateQueryRoles.join(', Query ')}; keep one workbook per role.</p>}
        <p>{preflight.loan_count ?? 0} explicit loan records discovered across {preflight.account_count ?? 0} borrower accounts. Search selects by loan number; it does not join by row position or name.</p>
      <h5>Priority blockers · first {priorityIssues.length} of {preflight.issue_count ?? priorityIssues.length}</h5>
      <ul>{priorityIssues.map((issue, i) => <li key={i}>{issue}</li>)}</ul>
      <p>{preflight.suggested_next_step ?? 'Review the extracted attributes and source relationships before processing.'}</p><p>{preflight.comparison ?? 'No previous validation comparison is available.'}</p>
      <small>Only the selected loan's related source rows are loaded. Cross-file relationships use explicit IDs; local validation is not a lending decision.</small>
      </details>
      {preflight.recognized_mtb && !preflight.requires_loan_number && !preflight.scoped_loan_id && <>
        <label>Search loan numbers<input aria-label="Search loan numbers" value={loanSearch} onChange={(event) => setLoanSearch(event.target.value)} placeholder="Enter a loan number, e.g. MTB-L001" /></label>
        <div className="guided-actions">
          <button type="button" className="button secondary" disabled={disabled || loanLoading} onClick={() => void loadLoans(0)}>Search loans</button>
          <label>Choose loan number<select ref={sourceLoanSelector} aria-label="Choose loan number" value={session.selected_loan_id ?? ''} disabled={disabled || loanLoading || !(loanTotal || (preflight.loan_count ?? 0))}
            onChange={(event) => {
              const loanId = event.target.value;
              if (loanId) void run('Selecting explicit loan number', async () => onSession(await api.selectLoan(session.id, loanId)));
            }}>
            <option value="">Select an explicit loan number</option>
            {session.selected_loan_id && !loanCandidates.some((item) => item.loan_id === session.selected_loan_id)
              && <option value={session.selected_loan_id}>{session.selected_loan_id} · selected</option>}
            {loanCandidates.map((item) => <option key={`${item.loan_id}:${item.account_id}`} value={item.loan_id}>
              {item.loan_id} · {item.account_id ? `${item.account_name ? `${item.account_name} · ` : ''}${item.account_id}` : 'Borrower relationship missing or ambiguous'}
            </option>)}
          </select></label>
          {loanCandidates.length < loanTotal && <button type="button" className="button secondary" disabled={disabled || loanLoading} onClick={() => void loadLoans(loanCandidates.length)}>Load more loans</button>}
        </div>
        {session.selected_loan_id && !session.selected_account_id && <p className="muted">This loan can be reviewed without a verified borrower. Guarantor records retain their roles; missing borrower evidence remains UNKNOWN.</p>}
        {requiresLoanSelection && !session.selected_loan_id && <small>{!queryFileSetValid ? 'Upload one workbook each for Query 1, Query 2, and Query 4 before preparing the review.' : 'Enter a loan number and load its related data before preparing the review.'}</small>}
      </>}
      {prepareReviewAction}
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
      {missingDemoSnapshot && <section role="status" aria-label="Saved report requires demo refresh">
        <h4>This saved report predates the TEST demo</h4>
        <p>No valid TEST demo snapshot. Commentary is historical; choose another loan and prepare a new review to refresh. Confirmed processing may incur charges.</p>
      </section>}
      <p id="loan-review-disclosure">Selecting a loan starts its Claude review automatically and confirms sending bounded normalized evidence to Anthropic; each new review may incur charges. Raw workbook rows are not sent. Saved reviews are reused. Opening this screen does not start a request, and failures are not retried automatically.</p>
      <label>Loan to explore<select ref={loanSelector} aria-label="Loan to explore" value={selected || ''} disabled={disabled || aiPending} onChange={(event) => {
        const loanId = event.target.value;
        setSelected(loanId);
        setRecordChoiceLoan(null);
        if (loanId) beginLoanReview(loanId);
      }} aria-describedby="loan-review-disclosure"><option value="">Select a loan to review with Claude</option>{loans.map((loan) => <option key={loan.loan_id} value={loan.loan_id}>{loan.loan_id}</option>)}</select></label>
      {recordChoiceLoan === selectedLoan?.loan_id && <section className="guided-confirmation" aria-label="Choose credit-report evidence">
        <h4>Found {recordCandidates.length} credit-score observations for this loan</h4>
        <p>These may be separate reports or conflicting copies of the same report. Which source record should Claude focus on?</p>
        {recordCandidates.map((item) => <label key={item.id}>
          <input type="radio" name="credit-record-choice" value={item.id} checked={preferredRecordRef === item.id} onChange={() => setPreferredRecordRef(item.id)} disabled={disabled || aiPending} />
          Report {item.record_id} · Score {String(item.value ?? 'Unknown')} · {item.workbook} · {item.sheet}, row {item.row}
          <small>{item.field}</small>
        </label>)}
        <p>Your selection is an unverified review preference—not a correction to source data. Conflicting scores and required report-selection checks remain unresolved. Continuing sends normalized context and the chosen evidence ID to Anthropic and may incur charges.</p>
        <button type="button" className="button primary" disabled={disabled || aiPending || !preferredRecordRef} onClick={() => {
          setRecordChoiceLoan(null);
          requestLoanReview(recordChoiceLoan, session.updated_at, preferredRecordRef);
        }}>Continue review with selected record</button>
      </section>}
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
      {latestAIReview && <div tabIndex={-1} aria-label="Selected-loan Claude review result">
        <section className="claude-explanation" aria-label="Selected-loan Claude rationale">
          <h4>Claude rationale · {latestAIReview.loan_id}</h4>
          {!showDemo && !missingDemoSnapshot && <p>Final lending decisions require authorized human review.</p>}
          <ClaudeExplanationText text={latestAIReview.analysis.explanation} />
          <details className="claude-audit" key={latestAIReview.id}><summary>Claude audit details · model, context and evidence</summary>
          <p className="claude-model">Model: {latestAIReview.model}</p>
          <p>Saved route: {latestAIReview.analysis.recommended_route} · Human review required.</p>
          <p>Loan IDs: {latestAIReview.analysis.loan_ids.join(', ')}</p>
          {missingDemoSnapshot && <p>Historical strict-workflow commentary — not the current TEST demo.</p>}
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
          </details>
        </section>
      </div>}
      {feedback && <p role="status">{feedback}</p>}
      {latestInspection?.inspection && <div tabIndex={-1} aria-label="Latest loan lookup result">
        <InspectionCard key={latestInspection.id} inspection={latestInspection.inspection} />
      </div>}
      <details className="secondary-actions"><summary>Review notes and acknowledgments</summary>
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
      </details>
    </div>}
  </section>;
}