import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import UnderwritingReport from './UnderwritingReport';
import type { ExpandedSpecEvaluation, LoanRecommendation, UnderwritingBatch } from './types';

const hugeUsd = '90071992547409931234567890.1234567890';

function loan(overrides: Partial<LoanRecommendation> = {}): LoanRecommendation {
  return {
    loan_id: 'loan-1', borrower_account_id: 'borrower-7', decision_status: 'Human Review', currency: 'USD', mode: 'PRODUCTION', policy_version: 'v4-usd',
    normalized_amounts_usd: { loan_requested_amount_usd: hugeUsd, ebitda_usd: '-0.00100', new_money_usd: null },
    normalized_data: {}, missing_inputs: ['risk_score'], conflicts: ['Tier inputs conflict'],
    recommendation_path: 'DATA_QUALITY_HOLD', eligibility_route: 'DATA_REVIEW', credit_assessment: 'MANUAL_REVIEW',
    authorization: 'NOT_ISSUED', strategy_branch: null, final_action: 'MANUAL_REVIEW', human_review_required: true,
    explanation: '<b>Deterministic explanation</b>', llm_error: null,
    rule_results: ['PASS', 'FAIL', 'UNKNOWN', 'NOT_APPLICABLE'].map((status, index) => ({
      rule_id: `R${index}`, status: status as LoanRecommendation['rule_results'][number]['status'], detail: `Check ${index}`,
      page: index === 0 ? 4 : null, policy_reference: index === 0 ? 'Draft p4' : null,
      evidence_refs: index === 0 ? ['cell-1', 'missing-ref'] : [],
    })),
    source_evidence: [{ id: 'cell-1', workbook: 'inputs.xlsx', sheet: 'Loans', row: 8, record_id: 'source-1', field: 'requested_amount', value: hugeUsd }],
    ...overrides,
  };
}

function batch(overrides: Partial<UnderwritingBatch> = {}): UnderwritingBatch {
  return {
    mode: 'PRODUCTION', currency: 'USD', policy_version: 'v4-usd', source_document: 'LLM_Loan_Approval_Design_Guide_v4_USD_Copilot.pdf',
    source_available: false, implementation_guide_source_document: 'MTB_LLM_Underwriting_Complete_Implementation_Guide.pdf',
    implementation_guide_source_sha256: '8a409bf6499129be8efe109d1a15f21ecfd95929b0674b993ab6d13c9c8e9467',
    implementation_guide_available: false, data_quality_issues: ['Join mapping not confirmed'], loan_results: [loan()], ...overrides,
  };
}

describe('V4 underwriting report', () => {
  it.each(['Approve', 'Decline', 'Refer'])('shows %s from the isolated TEST demo independently of strict review', (label) => {
    render(<UnderwritingReport batch={batch({ mode: 'TEST', loan_results: [loan({
      mode: 'TEST', final_action: 'NO_OPERATIONAL_ACTION', recommendation_path: 'SIMULATED_REVIEW',
      normalized_data: { demo_decision: { version: 'screenshot-test-demo-v1', label,
        simulation_only: true, authorization: 'NOT_ISSUED', selected_report_id: 'R1',
        rationale: 'SBSS 155 is below 160.', criteria: [] } },
    })] })} />);
    const region = within(screen.getByRole('region', { name: 'Simulation recommendation for loan-1' }));
    expect(region.getByText(label)).toBeInTheDocument();
    expect(region.getByText('SBSS 155 is below 160.')).toBeInTheDocument();
    expect(region.getByText(/report dates and completion are not checked/)).toBeInTheDocument();
    expect(region.getByText(/Not a final lending decision/)).toBeInTheDocument();
  });

  it.each([
    ['SIMULATED_APPROVE_PATH', 'Approve'],
    ['SIMULATED_DECLINE_PATH', 'Decline'],
    ['SIMULATED_REVIEW', 'Refer'],
  ] as const)('labels qualified TEST path %s as a simulation-only %s recommendation', (path, label) => {
    render(<UnderwritingReport batch={batch({ mode: 'TEST', loan_results: [loan({
      mode: 'TEST', recommendation_path: path, final_action: 'NO_OPERATIONAL_ACTION',
      missing_inputs: [], conflicts: [],
    })] })} />);
    const result = within(screen.getByRole('region', { name: 'Simulation recommendation for loan-1' }));
    expect(result.getByText(label)).toBeInTheDocument();
    expect(result.getByText(/Not a final lending decision/)).toBeInTheDocument();
  });

  it('does not promote a standalone approve candidate or incomplete TEST path into an approval', () => {
    render(<UnderwritingReport batch={batch({ mode: 'TEST', loan_results: [loan({
      mode: 'TEST', recommendation_path: 'SIMULATED_APPROVE_PATH', final_action: 'NO_OPERATIONAL_ACTION',
      normalized_data: { business_banking_workflow: { recommendation_candidate: 'APPROVE_CANDIDATE' } },
      missing_inputs: ['explicit_iso_as_of_date'], conflicts: [],
    })] })} />);
    const result = within(screen.getByRole('region', { name: 'Simulation recommendation for loan-1' }));
    expect(result.getByText('Refer')).toBeInTheDocument();
    expect(result.queryByText('Approve')).not.toBeInTheDocument();
  });

  it('displays Refer in PRODUCTION regardless of a diagnostic candidate', () => {
    render(<UnderwritingReport batch={batch({ loan_results: [loan({
      credit_assessment: 'SIMULATED_APPROVAL_CANDIDATE', missing_inputs: [], conflicts: [],
    })] })} />);
    expect(within(screen.getByRole('region', { name: 'Simulation recommendation for loan-1' })).getByText('Refer')).toBeInTheDocument();
  });

  it('renders raw characteristic key/value pairs without deriving date precision', async () => {
    render(<UnderwritingReport batch={batch({ loan_results: [loan({ normalized_data: {
      as_of_date: null,
      selected_report_id: null,
      characteristic_observations: {
        note: 'Raw source values only; unlinked fields are not inferred.',
        groups: [{ account_id: 'A1', loan_id: 'L1', party_role: 'borrower', response_id: null,
          response_link_status: 'NOT_EXPORTED_ON_CHARACTERISTIC_ROW', report_id: null,
          observations: [{ key: 'FileEstablishedDate', value: '19960700', source_record_id: 'C1', evidence_refs: ['cell-date'] },
            { key: 'OtherCharacteristic', value: 'false', source_record_id: 'C2', evidence_refs: ['cell-2'] }],
        }],
      },
    } })] })} />);

    const card = within(screen.getByRole('article', { name: 'Loan loan-1' }));
    await userEvent.click(card.getByText('Raw characteristic key/value observations · 2'));
    expect(card.getByText('FileEstablishedDate')).toBeVisible();
    expect(card.getByText('19960700')).toBeVisible();
    expect(card.getByText('OtherCharacteristic')).toBeVisible();
    expect(card.getByText('false')).toBeVisible();
    expect(card.getByText(/SBSS response: not explicitly linked/)).toBeVisible();
    expect(card.getByText(/Credit report: not explicitly linked/)).toBeVisible();
  });

  it('renders optional parent-isolated observations locally without coercion or HTML interpretation', async () => {
    const { container } = render(<UnderwritingReport batch={batch({ loan_results: [loan({ normalized_data: {
      observation_groups: [
        { account_id: 'A1', response_id: 'S1', report_id: 'R1', entity_id: 'E1', analysis_id: 'D1',
          indicators: [], characteristics: [{ key: '<script>unknown</script>', value: '0005', evidence_refs: ['cell-1'] }], evidence_refs: ['cell-1'] },
        { account_id: 'A1', response_id: 'S2', report_id: null, entity_id: 'E2', analysis_id: 'D2',
          indicators: [], characteristics: [{ key: '<script>unknown</script>', value: 'N' }, { key: 'date?', value: '19960700' },
            { key: 'flag?', value: 'false' }], evidence_refs: [] },
      ],
    } })] })} />);
    await userEvent.click(screen.getByText('Explicitly grouped source observations · 2 groups'));
    const first = within(screen.getByRole('region', { name: 'Observation group 1' }));
    const second = within(screen.getByRole('region', { name: 'Observation group 2' }));
    expect(first.getByText('S1')).toBeVisible();
    expect(first.getByText('R1')).toBeVisible();
    expect(first.getByText(/"value": "0005"/)).toBeVisible();
    expect(second.getByText('S2')).toBeVisible();
    expect(second.getByText('Unknown')).toBeVisible();
    for (const value of ['N', '19960700', 'false']) expect(second.getByText(new RegExp(`"value": "${value}"`))).toBeVisible();
    expect(second.queryByText('R1')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
  });

  it('shows expanded candidates, rule metadata and all reason statuses without issuing adverse action', async () => {
    const spec: ExpandedSpecEvaluation = {
      version: 'expanded-draft-1', source_document: 'expanded.pdf', source_sha256: 'synthetic-source-hash',
      source_available: true, eligibility_route: 'QUICK_APP', simulated_credit_route: 'SBA_APPROVAL_CANDIDATE',
      authorized_decision: 'NOT_ISSUED', candidate_reason_codes: ['BANKRUPTCY'],
      missing_inputs: ['approved_recency'], policy_conflicts: ['UNAPPROVED_DECLINE_REASON_CODES'],
      input_fingerprint_sha256: 'synthetic-input-hash', prompt_version: 'expanded-explanation-1',
      reviewer_id: null, decision_history: [], human_review_required: true,
      rule_results: [{ rule_id: 'KO-06', scope: 'bankruptcy', precedence: 3, version: 'expanded-draft-1',
        source_document: 'expanded.pdf', page: 13, predicate: 'Explicit discharge date', required_inputs: ['discharge_date'],
        missing_data_action: 'DATA_REVIEW', exception_path: 'Human review', decline_reason_codes: ['BANKRUPTCY'],
        status: 'FAIL', detail: 'Synthetic diagnostic only', evidence_refs: ['cell-1'] }],
      decline_reasons: (['PASS', 'FAIL', 'REVIEW', 'NOT_EVALUATED'] as const).map((status, index) => ({
        code: `REASON-${index}`, label: index === 1 ? '<script>untrusted</script>' : `Reason ${index}`,
        page: 8, source_document: 'expanded.pdf', status, disposition: 'MANUAL_REVIEW',
        detail: 'No denial issued', required_inputs: ['verified_evidence'], evidence_refs: ['cell-1'], adverse_action_code: null,
      })),
    };
    const { container } = render(<UnderwritingReport batch={batch({ loan_results: [loan({ expanded_spec: spec })] })} />);
    const section = within(screen.getByRole('region', { name: 'Expanded technical specification' }));
    expect(section.getByText('SBA_APPROVAL_CANDIDATE')).toBeInTheDocument();
    expect(section.getByText(/not approved adverse-action codes/)).toBeInTheDocument();
    await userEvent.click(section.getByText('Complete decline-reason inventory · 4 reasons'));
    for (let i = 0; i < 4; i++) expect(section.getByRole('region', { name: `Reason REASON-${i}` })).toBeVisible();
    expect(section.getByText(/<script>untrusted<\/script>/)).toBeVisible();
    expect(container.querySelector('script')).toBeNull();
    await userEvent.click(section.getByText('Expanded rule registry and audit · 1 rules'));
    expect(section.getByRole('region', { name: 'Expanded rule KO-06' })).toBeVisible();
    expect(section.getByText(/synthetic-input-hash/)).toBeVisible();
  });

  it('preserves exact USD precision, nullable amounts, loan identity and non-operational action', () => {
    render(<UnderwritingReport batch={batch()} />);
    const card = within(screen.getByRole('article', { name: 'Loan loan-1' }));
    expect(card.getByText(`USD ${hugeUsd}`)).toBeInTheDocument();
    expect(card.getByText('USD -0.00100')).toBeInTheDocument();
    expect(card.getByText('Unknown', { selector: 'dd' })).toBeInTheDocument();
    expect(card.getByText('borrower-7')).toBeInTheDocument();
    expect(card.getByText('Decision status: Human Review. No automated approval or denial is issued.')).toBeInTheDocument();
    expect(card.getByText('DATA_QUALITY_HOLD')).toBeInTheDocument();
    expect(card.getByText('DATA_REVIEW')).toBeInTheDocument();
    expect(card.getByText('MANUAL_REVIEW')).toBeInTheDocument();
    expect(card.getByText('NOT_ISSUED')).toBeInTheDocument();
    expect(card.getByText('Yes — authorized human review required')).toBeInTheDocument();
    expect(card.getByText('risk_score')).toBeInTheDocument();
    expect(card.getByText('Tier inputs conflict')).toBeInTheDocument();
    expect(screen.getByText(/V4 PDF source verification/)).toBeInTheDocument();
    expect(screen.getByText('9ed35356fdf8d7f638b6eb77c463df71f9a6dd17234a5fe31be251fda8ac18e7')).toBeInTheDocument();
    expect(screen.getByText(/7-page implementation guide verification/)).toBeInTheDocument();
    expect(screen.getByText('8a409bf6499129be8efe109d1a15f21ecfd95929b0674b993ab6d13c9c8e9467')).toBeInTheDocument();
    expect(screen.getByText('Join mapping not confirmed')).toBeInTheDocument();
    expect(screen.getByText(/PRODUCTION — manual review only/)).toBeInTheDocument();
  });

  it('renders ordered decision-flow validation and its evidence references', () => {
    render(<UnderwritingReport batch={batch({ loan_results: [loan({ normalized_data: {
      decision_flow: [
        { stage: 'Account and relationship validation', status: 'PASS', summary: 'Explicit account link verified.', evidence_refs: ['cell-1'] },
        { stage: 'Screenshot knockout screen', status: 'CANDIDATE_MATCH', summary: 'Candidate criteria matched: MAX_DELINQUENCY_PUBLIC_RECORDS_KNOCKOUT_CANDIDATE.', evidence_refs: ['cell-1'] },
        { stage: 'Authorization and final action', status: 'REVIEW_REQUIRED', summary: 'authorization=NOT_ISSUED; final action=MANUAL_REVIEW.', evidence_refs: [] },
      ],
    } })] })} />);
    const flow = within(screen.getByRole('region', { name: 'Ordered decision-flow validation' }));
    expect(flow.getByText(/1\. Account and relationship validation/)).toBeInTheDocument();
    expect(flow.getByText(/2\. Screenshot knockout screen/)).toBeInTheDocument();
    expect(flow.getByText('PASS')).toBeInTheDocument();
    expect(flow.getByText('CANDIDATE_MATCH')).toBeInTheDocument();
    expect(flow.getByText(/Explicit account link verified/)).toBeInTheDocument();
    expect(flow.getAllByText('Evidence references: cell-1')).toHaveLength(2);
    expect(flow.getByText(/unknown means not validated, not passed/)).toBeInTheDocument();
  });

  it('shows route, candidate assessment, authorization and guide diagnostics as separate non-operational outputs', async () => {
    render(<UnderwritingReport batch={batch({ source_available: true, implementation_guide_available: true, loan_results: [loan({
      eligibility_route: 'QUICK_APP', credit_assessment: 'SIMULATED_APPROVAL_CANDIDATE', authorization: 'NOT_ISSUED',
      strategy_branch: 'SBSS_GE_200_OWNERSHIP_GE_2_OTHER_NAICS',
      normalized_data: {
        sbss_bucket: 'GE_200', ownership_at_least_2_years: true,
        out_of_scope_diagnostics: { negative_news: 'NOT_EVALUATED' },
        implementation_guide_diagnostics: { audit: { input_fingerprint_sha256: 'synthetic-audit-hash' }, stages: {
          stage_0_schema_identity: 'PASS', stage_1_policy_approval_and_mode: 'DRAFT_UNAPPROVED_SIMULATION_ONLY',
          stage_2_product_eligibility_and_completeness: 'PASS', stage_3_hard_knockouts: 'PASS',
          stage_4_credit_strategy: 'CANDIDATE_MATCH', stage_5_non_operational_route: 'SIMULATED_APPROVAL_CANDIDATE',
          stage_6_explanation_and_audit: 'REQUIRED',
        } },
      },
    })] })} />);
    const card = within(screen.getByRole('article', { name: 'Loan loan-1' }));
    expect(card.getByText('QUICK_APP')).toBeInTheDocument();
    expect(card.getByText(/SIMULATED_APPROVAL_CANDIDATE — candidate\/review only/)).toBeInTheDocument();
    expect(card.getByText('NOT_ISSUED')).toBeInTheDocument();
    expect(card.getByText('SBSS_GE_200_OWNERSHIP_GE_2_OTHER_NAICS')).toBeInTheDocument();
    expect(card.getByText(/Guide diagnostics and candidate features are not credited rule outcomes/)).toBeInTheDocument();
    const summary = card.getByText('Implementation guide diagnostics · stages and normalized features');
    const diagnostics = summary.closest('details');
    expect(diagnostics).not.toHaveAttribute('open');
    await userEvent.click(summary);
    expect(within(diagnostics as HTMLElement).getByText('stage 0 schema identity')).toBeInTheDocument();
    expect(within(diagnostics as HTMLElement).getByText('stage 6 explanation and audit')).toBeInTheDocument();
    expect(within(diagnostics as HTMLElement).getByText('GE_200')).toBeInTheDocument();
    expect(within(diagnostics as HTMLElement).getByText(/synthetic-audit-hash/)).toBeInTheDocument();
    expect(within(diagnostics as HTMLElement).getByText(/NOT_EVALUATED/)).toBeInTheDocument();
    expect(screen.getByText(/Verified — pinned PDF available and hash matches/)).toBeInTheDocument();
    expect(screen.getByText(/Verified — pinned guide available and hash matches/)).toBeInTheDocument();
    expect(screen.queryByText(/operational approval/i)).not.toBeInTheDocument();
  });

  it('expands all exact rule statuses, nullable pages and resolved evidence cells without inventing citations', async () => {
    const { container } = render(<UnderwritingReport batch={batch()} />);
    const summary = screen.getByText('Rule trace and source cells · 4 checks');
    expect(summary.closest('details')).not.toHaveAttribute('open');
    await userEvent.click(summary);
    expect(summary.closest('details')).toHaveAttribute('open');
    for (const status of ['PASS', 'FAIL', 'UNKNOWN', 'NOT_APPLICABLE']) expect(screen.getByText(status)).toBeVisible();
    const rule = within(screen.getByRole('region', { name: 'Rule R0' }));
    expect(rule.getByText('PDF page 4')).toBeVisible();
    expect(rule.getByText('Policy reference: Draft p4')).toBeVisible();
    expect(rule.getByText('inputs.xlsx / Loans / row 8 / field requested_amount')).toBeVisible();
    expect(rule.getByText(hugeUsd)).toBeVisible();
    expect(rule.getByText('Record ID: source-1')).toBeVisible();
    expect(rule.getByText(/missing-ref — unresolved evidence reference/)).toBeVisible();
    expect(screen.getAllByText('Data diagnostic — no PDF page citation')).toHaveLength(3);
    expect(screen.getByText('<b>Deterministic explanation</b>')).toBeInTheDocument();
    expect(container.querySelector('b, script, img')).toBeNull();
  });

  it('shows batch data quality and zero loans without fabricated cards or pass claims', () => {
    render(<UnderwritingReport batch={batch({ loan_results: [], data_quality_issues: ['Schema-only workbook; zero data rows.'] })} />);
    expect(screen.getByText('Schema-only workbook; zero data rows.')).toBeInTheDocument();
    expect(screen.getByText('0 explicit loan records')).toBeInTheDocument();
    expect(screen.getByText(/No loans were fabricated/)).toBeInTheDocument();
    expect(screen.queryByRole('article')).not.toBeInTheDocument();
    expect(screen.queryByText('PASS')).not.toBeInTheDocument();
  });

  it('labels TEST paths as simulations with no operational action and displays source availability', () => {
    render(<UnderwritingReport batch={batch({ mode: 'TEST', source_available: true, implementation_guide_available: true, data_quality_issues: [], loan_results: [loan({
      mode: 'TEST', recommendation_path: 'SIMULATED_APPROVE_PATH', final_action: 'NO_OPERATIONAL_ACTION', borrower_account_id: null,
      eligibility_route: 'QUICK_APP', credit_assessment: 'SIMULATED_APPROVAL_CANDIDATE', authorization: 'NOT_ISSUED',
      normalized_amounts_usd: {}, missing_inputs: [], conflicts: [], rule_results: [], source_evidence: [], llm_error: 'Commentary unavailable.',
    })] })} />);
    expect(screen.getByText(/TEST — simulated paths only. No operational action./)).toBeInTheDocument();
    expect(screen.getByText('SIMULATED_APPROVE_PATH')).toBeInTheDocument();
    expect(screen.getByText('NO_OPERATIONAL_ACTION')).toBeInTheDocument();
    expect(screen.getByText('NOT_ISSUED')).toBeInTheDocument();
    expect(screen.getByText('Yes — authorized human review required')).toBeInTheDocument();
    expect(screen.getByText('Not provided')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Commentary unavailable.');
  });
});