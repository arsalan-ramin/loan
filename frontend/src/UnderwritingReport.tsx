import type { ExpandedSpecEvaluation, LoanRecommendation, SourceEvidence, UnderwritingBatch } from './types';
import InputReadiness from './InputReadiness';

const V4_SOURCE_SHA256 = '9ed35356fdf8d7f638b6eb77c463df71f9a6dd17234a5fe31be251fda8ac18e7';

function IssueList({ title, items }: { title: string; items: string[] }) {
  return <div className="policy-details"><h4>{title}</h4>{items.length
    ? <ul>{items.map((item, index) => <li key={index}>{item}</li>)}</ul>
    : <p>None reported.</p>}</div>;
}

function EvidenceCell({ evidence }: { evidence: SourceEvidence }) {
  return <div className="underwriting-evidence">
    <strong>{evidence.id}</strong>
    <p>{evidence.workbook} / {evidence.sheet} / row {evidence.row} / field {evidence.field}</p>
    {evidence.record_id !== null && <p>Record ID: {evidence.record_id}</p>}
    <pre>{evidence.value === null ? 'Unknown' : String(evidence.value)}</pre>
  </div>;
}

function displayDiagnostic(value: unknown): string {
  if (value === null || value === undefined) return 'Unknown';
  if (typeof value === 'string') return value;
  if (typeof value === 'boolean' || typeof value === 'number') return String(value);
  return JSON.stringify(value);
}

function ExpandedSpecReport({ spec }: { spec: ExpandedSpecEvaluation }) {
  return <section aria-label="Expanded technical specification">
    <h5>Expanded specification · draft simulation diagnostics</h5>
    <p>Internal diagnostic reason codes are not approved adverse-action codes. No denial, notice or credit offer issued.</p>
    <dl className="policy-source">
      <div><dt>Expanded eligibility</dt><dd>{spec.eligibility_route}</dd></div>
      <div><dt>Expanded simulated credit route</dt><dd>{spec.simulated_credit_route}</dd></div>
      <div><dt>Expanded authorized decision</dt><dd>{spec.authorized_decision}</dd></div>
      <div><dt>Expanded source verification</dt><dd>{spec.source_available ? 'Verified draft source' : 'Not verified — not evaluated'}</dd></div>
      <div><dt>Expanded source</dt><dd>{spec.source_document}</dd></div>
      <div><dt>Expanded SHA-256</dt><dd>{spec.source_sha256}</dd></div>
    </dl>
    <IssueList title="Expanded candidate reason codes" items={spec.candidate_reason_codes} />
    <IssueList title="Expanded missing inputs" items={spec.missing_inputs} />
    <IssueList title="Expanded policy conflicts" items={spec.policy_conflicts} />
    <details><summary>Complete decline-reason inventory · {spec.decline_reasons.length} reasons</summary>
      {spec.decline_reasons.map((reason) => <section key={reason.code} aria-label={`Reason ${reason.code}`}>
        <h6>{reason.code} · {reason.label}</h6>
        <p>{reason.status} · {reason.disposition} · {reason.source_document} p.{reason.page}</p>
        <p>{reason.detail}</p>
        <p>Required inputs: {reason.required_inputs.join(', ') || 'None — not used or out of scope'}</p>
        <p>Evidence: {reason.evidence_refs.join(', ') || 'No evidence supplied; no clearance claimed'}</p>
      </section>)}
    </details>
    <details><summary>Expanded rule registry and audit · {spec.rule_results.length} rules</summary>
      {spec.rule_results.map((rule) => <section key={rule.rule_id} aria-label={`Expanded rule ${rule.rule_id}`}>
        <h6>{rule.rule_id} · {rule.status}</h6>
        <p>Stage {rule.precedence} · {rule.scope} · {rule.source_document} p.{rule.page}</p>
        <p>{rule.predicate}</p><p>{rule.detail}</p>
        <p>Missing data: {rule.missing_data_action} · Exception: {rule.exception_path}</p>
        <p>Evidence: {rule.evidence_refs.join(', ') || 'No source evidence supplied'}</p>
      </section>)}
      <p>Input SHA-256: {spec.input_fingerprint_sha256}</p>
      <p>Version: {spec.version} · Prompt version: {spec.prompt_version} · Reviewer: not assigned</p>
    </details>
  </section>;
}

function LoanCard({ loan }: { loan: LoanRecommendation }) {
  const evidenceById = new Map(loan.source_evidence.map((item) => [item.id, item]));
  const guide = loan.normalized_data.implementation_guide_diagnostics;
  const guideDiagnostics = guide && typeof guide === 'object' ? guide as Record<string, unknown> : {};
  const stages = guideDiagnostics.stages && typeof guideDiagnostics.stages === 'object'
    ? guideDiagnostics.stages as Record<string, unknown> : {};
  const decisionFlow = Array.isArray(loan.normalized_data.decision_flow)
    ? loan.normalized_data.decision_flow as Array<{ stage: string; status: string; summary: string; evidence_refs: string[] }>
    : [];
  const diagnosticFeatures = Object.entries(loan.normalized_data).filter(([key]) =>
    key !== 'implementation_guide_diagnostics' && key !== 'out_of_scope_diagnostics' && key !== 'decision_flow');
  const workflow = loan.normalized_data.business_banking_workflow as { eligibility?: string; recommendation_candidate?: string; stages?: Array<{ stage: string; status: string; summary: string }> } | undefined;
  return <article className="underwriting-loan" aria-label={`Loan ${loan.loan_id}`}>
    <h4>Loan {loan.loan_id}</h4>
    {loan.normalized_data.behavioral_program != null && <p>Source underwriting program: {displayDiagnostic(loan.normalized_data.behavioral_program)}. Retained as program evidence; not automatically substituted for the requested application path.</p>}
    {workflow && <section aria-label="Business banking existing-process workflow">
      <h5>Business banking · existing-process decision flow</h5>
      <p>Diagram eligibility: {workflow.eligibility} · Non-operational candidate: {workflow.recommendation_candidate}</p>
      <p>USD 250K group-credit filter is separate from the conflicting draft Tier I diagnostic. No approved policy or final decision is inferred.</p>
      {workflow.stages?.map((step) => <div className="policy-result" key={step.stage}>
        <h6>{step.stage} · {step.status}</h6><p>{step.summary}</p>
      </div>)}
    </section>}
    <dl className="policy-source">
      <div><dt>Borrower account ID</dt><dd>{loan.borrower_account_id ?? 'Not provided'}</dd></div>
      <div><dt>Recommendation path (non-operational)</dt><dd>{loan.recommendation_path}</dd></div>
      <div><dt>Eligibility route</dt><dd>{loan.eligibility_route ?? 'Not reported'}</dd></div>
      <div><dt>Credit assessment candidate</dt><dd>{loan.credit_assessment ?? 'Not reported'} — candidate/review only</dd></div>
      <div><dt>Authorization</dt><dd>{loan.authorization ?? 'NOT_ISSUED'}</dd></div>
      {loan.strategy_branch && <div><dt>Implementation guide strategy branch (diagnostic)</dt><dd>{loan.strategy_branch}</dd></div>}
      <div><dt>Final action</dt><dd>{loan.final_action}</dd></div>
      <div><dt>Human review required</dt><dd>{loan.human_review_required ? 'Yes — authorized human review required' : 'Not reported'}</dd></div>
      <div><dt>Mode / policy version</dt><dd>{loan.mode} / {loan.policy_version}</dd></div>
    </dl>
    <section aria-label="Ordered decision-flow validation">
      <h5>Screenshot decision-flow validation</h5>
      <p className="muted">The simulated branch uses only the supplied screenshot's criteria. A simulated candidate is not an actual approval or rejection. Each stage shows its result and source-cell evidence; unknown means not validated, not passed.</p>
      {!decisionFlow.length && <p>No ordered decision-flow trace was returned.</p>}
      {decisionFlow.map((step, index) => <div className="policy-result" key={`${step.stage}-${index}`}>
        <h6>{index + 1}. {step.stage} · <span className="underwriting-status">{step.status}</span></h6>
        <p>{step.summary}</p>
        <p>Evidence references: {step.evidence_refs?.join(', ') || 'No source-cell references supplied'}</p>
      </div>)}
    </section>
    <details className="underwriting-trace">
      <summary>Implementation guide diagnostics · stages and normalized features</summary>
      <p className="muted">Guide diagnostics and candidate features are not credited rule outcomes or operational decisions.</p>
      <h5>Guide stage diagnostics</h5>
      {!Object.keys(stages).some((key) => key.startsWith('stage_')) && <p>No guide stage diagnostics reported.</p>}
      <dl className="policy-source">
        {Object.entries(stages).filter(([key]) => /^stage_[0-6]_/.test(key)).map(([stage, value]) => <div key={stage}>
          <dt>{stage.replaceAll('_', ' ')}</dt><dd>{displayDiagnostic(value)}</dd>
        </div>)}
      </dl>
      <h5>Normalized feature diagnostics (guide diagnostic/candidate; not credited rule outcomes)</h5>
      {!diagnosticFeatures.length && <p>No normalized feature diagnostics reported.</p>}
      <dl className="policy-source">
        {diagnosticFeatures.map(([field, value]) => <div key={field}>
          <dt>{field}</dt><dd>{displayDiagnostic(value)}</dd>
        </div>)}
      </dl>
      <h5>Guide audit provenance</h5>
      <pre>{displayDiagnostic(guideDiagnostics.audit)}</pre>
      <h5>Out-of-scope checks — not evaluated</h5>
      <pre>{displayDiagnostic(loan.normalized_data.out_of_scope_diagnostics)}</pre>
    </details>
    <h5>Exact USD amounts</h5>
    <dl className="policy-source underwriting-amounts">
      {Object.entries(loan.normalized_amounts_usd).map(([field, value]) => <div key={field}>
        <dt>{field}</dt><dd>{value === null ? 'Unknown' : `${loan.currency} ${value}`}</dd>
      </div>)}
    </dl>
    {!Object.keys(loan.normalized_amounts_usd).length && <p>No normalized amounts reported.</p>}
    <InputReadiness items={loan.missing_inputs} rules={loan.rule_results} />
    <IssueList title="Conflicts" items={loan.conflicts} />
    {loan.expanded_spec && <ExpandedSpecReport spec={loan.expanded_spec} />}
    <p className="underwriting-explanation">{loan.explanation}</p>
    <p className="muted">Deterministic per-loan explanation; not LLM-generated.</p>
    {loan.llm_error && <p className="inline-error" role="alert">{loan.llm_error}</p>}
    <details className="underwriting-trace">
      <summary>Rule trace and source cells · {loan.rule_results.length} checks</summary>
      {!loan.rule_results.length && <p>No rule results reported.</p>}
      {loan.rule_results.map((rule, index) => <section className="policy-result" key={`${rule.rule_id}-${index}`} aria-label={`Rule ${rule.rule_id}`}>
        <h5>{rule.rule_id} <span className={`underwriting-status ${rule.status.toLowerCase()}`}>{rule.status}</span></h5>
        <p>{rule.page === null ? 'Data diagnostic — no PDF page citation' : `PDF page ${rule.page}`}</p>
        {rule.policy_reference && <p>Policy reference: {rule.policy_reference}</p>}
        <p>{rule.detail}</p>
        <h6>Evidence references — resolved source cells</h6>
        {!rule.evidence_refs.length && <p>No evidence references reported.</p>}
        {rule.evidence_refs.map((ref, refIndex) => {
          const evidence = evidenceById.get(ref);
          return evidence ? <EvidenceCell key={`${ref}-${refIndex}`} evidence={evidence} />
            : <p className="document-warning" key={`${ref}-${refIndex}`}>{ref} — unresolved evidence reference; no source cell supplied.</p>;
        })}
      </section>)}
      <h5>All source evidence</h5>
      {!loan.source_evidence.length && <p>No source cells reported.</p>}
      {loan.source_evidence.map((item, index) => <EvidenceCell key={`${item.id}-${index}`} evidence={item} />)}
    </details>
  </article>;
}

export default function UnderwritingReport({ batch }: { batch: UnderwritingBatch }) {
  return <section className="policy-route-card underwriting-report" aria-labelledby="underwriting-report-heading">
    <span className="eyebrow">V4 · NON-OPERATIONAL UNDERWRITING</span>
    <h3 id="underwriting-report-heading">Per-loan underwriting report</h3>
    <p className="policy-disclaimer"><strong>{batch.mode === 'TEST'
      ? 'TEST — simulated paths only. No operational action.'
      : 'PRODUCTION — manual review only. No automated lending decision.'} Authorized human review required.</strong></p>
    <dl className="policy-source">
      <div><dt>Mode</dt><dd>{batch.mode}</dd></div>
      <div><dt>Currency</dt><dd>{batch.currency} — actual dollars; exact decimal strings</dd></div>
      <div><dt>Policy version</dt><dd>{batch.policy_version}</dd></div>
      <div><dt>Source document</dt><dd>{batch.source_document}</dd></div>
      <div><dt>V4 PDF source verification</dt><dd>{batch.source_available ? 'Verified — pinned PDF available and hash matches' : 'Not verified — source unavailable or hash mismatch'}</dd></div>
      <div><dt>V4 PDF SHA-256</dt><dd>{V4_SOURCE_SHA256}</dd></div>
      <div><dt>Implementation guide</dt><dd>{batch.implementation_guide_source_document ?? 'Not reported'}</dd></div>
      <div><dt>7-page implementation guide verification</dt><dd>{batch.implementation_guide_available ? 'Verified — pinned guide available and hash matches' : 'Not verified — guide unavailable or hash mismatch'}</dd></div>
      <div><dt>Implementation guide SHA-256</dt><dd>{batch.implementation_guide_source_sha256 ?? 'Not reported'}</dd></div>
      {batch.expanded_spec_source_document && <>
        <div><dt>Expanded technical specification</dt><dd>{batch.expanded_spec_source_document}</dd></div>
        <div><dt>Expanded source verification</dt><dd>{batch.expanded_spec_available ? 'Verified — pinned 14-page draft' : 'Not verified'}</dd></div>
        <div><dt>Expanded source SHA-256</dt><dd>{batch.expanded_spec_source_sha256}</dd></div>
      </>}
    </dl>
    <p className="muted">Both PDFs are draft evidence and do not confer lending authority. Eligibility routes and credit-assessment candidates are separate from authorization, which is always NOT_ISSUED. This report performs no lending approval or denial.</p>
    <IssueList title="Batch data quality issues" items={batch.data_quality_issues} />
    <IssueList title="Unmapped source columns" items={batch.unmapped_source_fields ?? []} />
    <IssueList title="Canonical inputs unavailable in source files" items={batch.unavailable_inputs ?? []} />
    <IssueList title="Unresolved source relationships" items={batch.mapping_warnings ?? []} />
    <p>{batch.loan_results.length} explicit loan records</p>
    {!batch.loan_results.length && <p className="document-warning">No explicit loan records. No loans were fabricated from source rows or empty templates.</p>}
    {batch.loan_results.map((loan, index) => <LoanCard key={`${loan.loan_id}-${index}`} loan={loan} />)}
  </section>;
}