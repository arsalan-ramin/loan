import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import GuidedAssistant, { InspectionCard } from './GuidedAssistant';
import type { LoanAIReview, LoanInspection, LoanRecommendation, Session } from './types';

function session(): Session {
  return { id: 's1', title: 'Review', mode: 'PRODUCTION', created_at: 'today', updated_at: 'revision1',
    status: 'ready', messages: [], documents: [{ id: 'd1', name: 'evidence.txt', size: 10, extension: '.txt', metadata: {} }],
    rules: [], steps: [], report: null, error: null };
}

function props(active = session()) {
  return { session: active, disabled: false, run: async (_label: string, work: () => Promise<void>) => work(),
    onSession: vi.fn(), onUpload: vi.fn(), onReport: vi.fn(), onDownload: vi.fn(), onAsk: vi.fn() };
}

function demoSession(label = 'Decline'): Session {
  return { ...session(), mode: 'TEST', status: 'completed', report: {
    id: 'demo-report', created_at: 'today', decision: 'needs_review', summary: 'Review', document_count: 1, findings: [],
    underwriting: { mode: 'TEST', currency: 'USD', policy_version: 'v4', source_document: 'LLM_Loan_Approval_Design_Guide_v4_USD_Copilot.pdf', source_available: false,
      data_quality_issues: [], loan_results: [{
        loan_id: 'L1', borrower_account_id: 'A1', decision_status: 'Human Review', currency: 'USD', mode: 'TEST', policy_version: 'v4',
        normalized_amounts_usd: {}, normalized_data: { demo_decision: { version: 'screenshot-test-demo-v1', simulation_only: true,
          authorization: 'NOT_ISSUED', label, rationale: 'SBSS 155 is below 160.', source_evidence: [{ id: 'demo-cell', value: '000155' }] } },
        rule_results: [], source_evidence: [], missing_inputs: [], conflicts: [], recommendation_path: 'SIMULATED_REVIEW',
        final_action: 'NO_OPERATIONAL_ACTION', human_review_required: true, explanation: 'Strict trace retained', llm_error: null,
      }] },
  } };
}

describe('Guided assistant workflow', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(session()), { status: 200 }))));

  it('shows only supplied matched reason labels in the demo summary', () => {
    const active = demoSession();
    const demo = active.report!.underwriting!.loan_results[0].normalized_data.demo_decision as Record<string, unknown>;
    demo.matched_reasons = [{ label: 'Derogatory personal credit', source: 'SUPPLIED_DECLINE_REASON_CATALOG' }];
    render(<GuidedAssistant {...props(active)} />);
    const summary = within(screen.getByRole('region', { name: 'TEST demo summary for L1' }));
    expect(summary.getByText('Matched reasons')).toBeVisible();
    expect(summary.getByText('Derogatory personal credit')).toBeVisible();
    expect(summary.queryByText('Bankruptcy')).not.toBeInTheDocument();
  });

  it.each(['Approve', 'Decline', 'Refer'])('leads with the selected TEST demo %s and preserves its collapsed evidence', async (label) => {
    const active = demoSession(label);
    const p = props(active);
    render(<GuidedAssistant {...p} />);
    const summary = screen.getByRole('region', { name: 'TEST demo summary for L1' });
    expect(summary).toHaveTextContent(`${label} · Loan L1`);
    expect(within(summary).getByText('TEST demo • no lending action')).toBeVisible();
    expect(within(summary).getByText('SBSS 155 is below 160.')).toBeVisible();
    expect(screen.queryByText(/^MANUAL_REVIEW/)).not.toBeInTheDocument();
    const details = within(summary).getByText('Demo criteria and source evidence').closest('details')!;
    expect(details).not.toHaveAttribute('open');
    await userEvent.click(within(summary).getByText('Demo criteria and source evidence'));
    expect(within(details).getByText(/000155/)).toBeVisible();
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each(['version', 'simulation_only', 'authorization', 'label', 'production'])('does not present an invalid or production demo as a recommendation: %s', (field) => {
    const active = demoSession();
    if (field === 'production') {
      active.report!.underwriting!.mode = 'PRODUCTION';
      active.report!.underwriting!.loan_results[0].mode = 'PRODUCTION';
    } else {
      (active.report!.underwriting!.loan_results[0].normalized_data.demo_decision as Record<string, unknown>)[field] = 'invalid';
    }
    render(<GuidedAssistant {...props(active)} />);
    expect(screen.queryByText('TEST demo • no lending action')).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Decline · Loan L1' })).not.toBeInTheDocument();
    if (field === 'production') expect(screen.getByText(/PRODUCTION — manual review only/)).toBeVisible();
    expect(fetch).not.toHaveBeenCalled();
  });

  it('keeps focus and viewport stable when saved Claude reviews and processing states arrive', () => {
    const scroll = vi.fn();
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, writable: true, value: scroll });
    const active = demoSession();
    const p = props(active);
    const view = render(<GuidedAssistant {...p} />);
    const selector = screen.getByRole('combobox', { name: 'Loan to explore' });
    selector.focus();
    const review: LoanAIReview = { id: 'saved-ai', created_at: 'today', loan_id: 'L1', report_id: 'demo-report', model: 'saved-model',
      context_note: 'Saved context', analysis: { recommended_route: 'MANUAL_REVIEW', decision_recommendation: null, confidence: null,
        requires_human_approval: true, loan_ids: ['L1'], explanation: 'Original arbitrary model wording remains intact.',
        positive_factors: ['Saved factor'], risk_factors: [], missing_information: [], triggered_rules: ['rule-1'], evidence_refs: ['cell-1'] } };
    view.rerender(<GuidedAssistant {...p} session={{ ...active, loan_ai_reviews: [review] }} />);
    expect(selector).toHaveFocus();
    expect(screen.getByText('Original arbitrary model wording remains intact.')).toBeVisible();
    for (const text of ['Model: saved-model', 'Saved context', 'Saved factor', 'Failed-rule citations: rule-1']) {
      expect(screen.getByText(text)).not.toBeVisible();
    }
    view.rerender(<GuidedAssistant {...p} session={{ ...active, status: 'processing' }} disabled />);
    view.rerender(<GuidedAssistant {...p} session={{ ...active, updated_at: 'new-revision', loan_ai_reviews: [review] }} />);
    expect(scroll).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it('runs local validation without processing or sending a chat request', async () => {
    const p = props(); render(<GuidedAssistant {...p} />);
    await userEvent.click(screen.getByText('File tools and report downloads'));
    await userEvent.click(screen.getByRole('button', { name: 'Validate files locally' }));
    expect(fetch).toHaveBeenCalledExactlyOnceWith('/api/sessions/s1/preflight', { method: 'POST' });
    await waitFor(() => expect(p.onSession).toHaveBeenCalled());
  });

  it('requires explicit charge-disclosing confirmation and binds it to the current revision', async () => {
    const focus = vi.spyOn(HTMLElement.prototype, 'focus');
    const p = props(); render(<GuidedAssistant {...p} />);
    await userEvent.click(screen.getByRole('button', { name: 'Prepare review' }));
    expect(fetch).not.toHaveBeenCalled();
    const confirmation = within(screen.getByRole('group', { name: 'Confirm external processing' }));
    expect(screen.getByRole('group', { name: 'Confirm external processing' })).toHaveFocus();
    expect(focus).toHaveBeenLastCalledWith({ preventScroll: true });
    focus.mockRestore();
    expect(screen.getByText('Workflow and processing details').closest('details')).not.toHaveAttribute('open');
    await userEvent.click(screen.getByText('Workflow and processing details'));
    expect(screen.getByRole('list', { name: 'Review steps' })).toBeInTheDocument();
    expect(confirmation.getByText(/may incur charges/)).toBeInTheDocument();
    await userEvent.click(confirmation.getByRole('button', { name: 'Confirm and process' }));
    expect(fetch).toHaveBeenCalledWith('/api/sessions/s1/assistant/process', expect.objectContaining({ method: 'POST', body: JSON.stringify({ confirmed: true, expected_updated_at: 'revision1' }) }));
  });

  it('requires local validation and an explicit loan-number selection for MTB processing', async () => {
    const active = session();
    active.documents = [1, 2, 4].map((role) => ({ id: `d${role}`, name: `MTB_Query_${role}.xlsx`, size: 10, extension: '.xlsx', metadata: {} }));
    active.preflight = { created_at: 'now', input_fingerprint: 'hash', recognized_mtb: true, query_roles: [1, 2, 4],
      missing_query_roles: [], duplicate_query_roles: [], query_file_set_valid: true,
      document_count: 3, attribute_count: 2, attributes: ['Id', 'Loan_ID'],
      loan_count: 2, account_count: 1, issue_count: 0,
      priority_issues: [], suggested_next_step: 'Select account', comparison: 'First', external_processing: false };
    const selected = { ...active, selected_account_id: 'A1', selected_loan_id: 'MTB-L003' };
    const fetchMock = vi.fn(async (url: string) => {
      const body = url.includes('/loans?')
        ? { results: [{ loan_id: 'MTB-L003', account_id: 'A1', account_name: 'Client A' }], total: 1, offset: 0, limit: 50, loan_count: 2 }
        : url.endsWith('/loan') ? selected : active;
      return new Response(JSON.stringify(body), { status: 200 });
    });
    vi.stubGlobal('fetch', fetchMock);
    const p = props(active); const view = render(<GuidedAssistant {...p} />);
    await screen.findByRole('option', { name: /MTB-L003 · Client A · A1/ });
    await userEvent.type(screen.getByRole('textbox', { name: 'Search loan numbers' }), 'MTB-L003');
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/sessions/s1/loans?q=MTB-L003&offset=0&limit=50', undefined));
    const prepare = screen.getByRole('button', { name: 'Prepare review' });
    expect(prepare).toBeDisabled();
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Choose loan number' }), 'MTB-L003');
    expect(fetchMock).toHaveBeenCalledWith('/api/sessions/s1/loan', expect.objectContaining({ method: 'PUT', body: JSON.stringify({ loan_id: 'MTB-L003' }) }));
    view.rerender(<GuidedAssistant {...p} session={selected} />);
    expect(prepare).toBeEnabled();
    const loanSelector = screen.getByRole('combobox', { name: 'Choose loan number' });
    expect(loanSelector.compareDocumentPosition(prepare) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getAllByRole('button', { name: 'Prepare review' })).toHaveLength(1);
    expect(screen.getByText('Validation details, attributes and issues').closest('details')).not.toHaveAttribute('open');
    expect(screen.getByRole('textbox', { name: 'Search loan numbers' })).toBeVisible();
    expect(loanSelector).toBeVisible();
    expect(prepare).toBeVisible();
  });

  it('cancels confirmation and clears stale confirmations after input revisions change', async () => {
    const p = props(); const view = render(<GuidedAssistant {...p} />);
    await userEvent.click(screen.getByRole('button', { name: 'Prepare review' }));
    await userEvent.click(screen.getByRole('button', { name: 'Cancel review' }));
    expect(screen.queryByRole('group', { name: 'Confirm external processing' })).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Prepare review' }));
    view.rerender(<GuidedAssistant {...p} session={{ ...p.session, updated_at: 'revision2' }} />);
    expect(screen.queryByRole('group', { name: 'Confirm external processing' })).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each([false, true])('chooses another loan from a completed report with local-only recovery: %s', async (missingPreflight) => {
    const active = session();
    active.status = 'completed';
    active.selected_account_id = 'A1';
    active.documents = [1, 2, 4].map((role) => ({ id: `d${role}`, name: `MTB_Query_${role}.xlsx`, size: 10, extension: '.xlsx', metadata: {} }));
    const preflight: NonNullable<Session['preflight']> = {
      created_at: 'now', input_fingerprint: 'same-files', recognized_mtb: true, query_roles: [1, 2, 4],
      missing_query_roles: [], duplicate_query_roles: [], query_file_set_valid: true,
      document_count: 3, attribute_count: 0, attributes: [], loan_count: 2, account_count: 2,
      issue_count: 0, priority_issues: [], suggested_next_step: 'Select account', comparison: 'Same files', external_processing: false,
    };
    active.preflight = missingPreflight ? null : preflight;
    active.report = { id: 'r1', created_at: 'today', decision: 'needs_review', summary: 'Account A review', document_count: 3,
      findings: [], underwriting: { mode: 'TEST', currency: 'USD', policy_version: 'v4', source_document: 'LLM_Loan_Approval_Design_Guide_v4_USD_Copilot.pdf',
        source_available: false, data_quality_issues: [], loan_results: [{
          loan_id: 'L1', borrower_account_id: 'A1', decision_status: 'Human Review', currency: 'USD', mode: 'TEST', policy_version: 'v4',
          normalized_amounts_usd: {}, normalized_data: {}, rule_results: [], source_evidence: [], missing_inputs: [], conflicts: [],
          recommendation_path: 'SIMULATED_REVIEW', final_action: 'NO_OPERATIONAL_ACTION', human_review_required: true,
          explanation: 'Saved account A result', llm_error: null,
        }] } };
    const recovered = { ...active, preflight, updated_at: 'revision2' };
    const switched: Session = { ...recovered, selected_account_id: 'A2', selected_loan_id: 'L2', status: 'ready', report: null, steps: [], updated_at: 'revision3' };
    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith('/preflight')) return new Response(JSON.stringify(recovered));
      if (url.includes('/loans?')) return new Response(JSON.stringify({ results: [
        { loan_id: 'L1', account_id: 'A1', account_name: 'Client A' },
        { loan_id: 'L2', account_id: 'A2', account_name: 'Client B' },
      ], total: 2, offset: 0, limit: 50, loan_count: 2 }));
      if (url.endsWith('/loan')) return new Response(JSON.stringify(switched));
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    const p = props(active);
    const view = render(<GuidedAssistant {...p} />);
    p.onSession.mockImplementation((updated: Session) => view.rerender(<GuidedAssistant {...p} session={updated} />));
    await userEvent.click(screen.getByRole('button', { name: 'Choose another loan' }));
    const selector = await screen.findByRole('combobox', { name: 'Choose loan number' });
    await waitFor(() => expect(selector).toHaveFocus());
    expect(screen.getByRole('combobox', { name: 'Loan to explore' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Prepare review' })).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([url]) => url.endsWith('/preflight'))).toHaveLength(missingPreflight ? 1 : 0);
    await userEvent.selectOptions(selector, 'L2');
    await waitFor(() => expect(p.onSession).toHaveBeenLastCalledWith(switched));
    expect(screen.queryByRole('combobox', { name: 'Loan to explore' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Prepare review' })).toBeEnabled();
    expect(selector).toHaveValue('L2');
    expect(p.onUpload).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls.every(([url]) => url.endsWith('/preflight') || url.endsWith('/loan') || url.includes('/loans?'))).toBe(true);
    expect(screen.queryByRole('group', { name: 'Confirm external processing' })).not.toBeInTheDocument();
  });

  it('shows escaped priority blockers and comparison without claiming approval', () => {
    const active = session();
    active.preflight = { created_at: 'now', input_fingerprint: 'hash', recognized_mtb: true, query_roles: [1],
      missing_query_roles: [2, 4], duplicate_query_roles: [], query_file_set_valid: false,
      document_count: 1, attribute_count: 2, attributes: ['Id', 'Loan_ID'],
      loan_count: 0, account_count: 0, issue_count: 20,
      priority_issues: ['<script>missing borrower</script>'], suggested_next_step: 'Replace source', comparison: 'Previous issue count 30; current 20', external_processing: false };
    const { container } = render(<GuidedAssistant {...props(active)} />);
    expect(screen.getByText('<script>missing borrower</script>')).toBeInTheDocument();
    expect(screen.getByText(/first 1 of 20/)).toBeInTheDocument();
    expect(screen.getByText(/Previous issue count 30/)).toBeInTheDocument();
    expect(screen.queryByText(/Missing Query roles/)).not.toBeInTheDocument();
    expect(container.querySelector('script')).toBeNull();
  });

  it('renders older persisted preflight results with missing optional fields', () => {
    const active = session();
    active.documents = [1, 2, 4].map((role) => ({ id: `d${role}`, name: `MTB_Query_${role}.xlsx`, size: 10, extension: '.xlsx', metadata: {} }));
    active.preflight = {
      created_at: 'now', input_fingerprint: 'legacy-hash', recognized_mtb: true, query_roles: [1, 2, 4],
      document_count: 3, loan_count: 0, account_count: 0, issue_count: 0,
      suggested_next_step: 'Review source files', comparison: 'Legacy validation', external_processing: false,
    } as NonNullable<Session['preflight']>;

    render(<GuidedAssistant {...props(active)} />);

    expect(screen.getByText('0 distinct column attributes extracted.')).toBeInTheDocument();
    expect(screen.getByText('Priority blockers · first 0 of 0')).toBeInTheDocument();
    expect(screen.getByText('Legacy validation')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Prepare review' })).toBeInTheDocument();
  });

  it('separates unmapped source headers from canonical inputs absent from the export', () => {
    const active = session();
    active.preflight = {
      created_at: 'now', input_fingerprint: 'coverage-hash', recognized_mtb: true,
      query_roles: [1, 2, 4], missing_query_roles: [], duplicate_query_roles: [], query_file_set_valid: true,
      document_count: 3, attribute_count: 4, attributes: ['Id', 'LLC_BI__Loan__c', 'Unknown_Field__c', 'nFUSE__Account__c'],
      unmapped_attribute_count: 1, unmapped_attributes: ['Unknown_Field__c'],
      unavailable_inputs: ['Explicit EBITDA (EBIT is not interchangeable)', 'Credit report date'],
      mapping_warnings: ['UNMAPPED_RELATION:Indicator:analysis_id'],
      loan_count: 1, account_count: 1, issue_count: 0, priority_issues: [],
      suggested_next_step: 'Select account', comparison: 'First', external_processing: false,
    };

    render(<GuidedAssistant {...props(active)} />);

    expect(screen.getByText(/1 source columns currently unmapped/)).toBeInTheDocument();
    expect(screen.getAllByText('Unknown_Field__c')).toHaveLength(2);
    expect(screen.getByText('Explicit EBITDA (EBIT is not interchangeable)')).toBeInTheDocument();
    expect(screen.getByText('Credit report date')).toBeInTheDocument();
    expect(screen.getByText('UNMAPPED_RELATION:Indicator:analysis_id')).toBeInTheDocument();
  });

  it('disables local actions during processing and shows step progress', async () => {
    const active = { ...session(), status: 'processing' as const, steps: [{ agent: 'Extraction agent', status: 'running' as const, detail: 'Reading files' }] };
    render(<GuidedAssistant {...props(active)} disabled />);
    await userEvent.click(screen.getByText('File tools and report downloads'));
    expect(screen.getByRole('button', { name: 'Validate files locally' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Prepare review' })).toBeDisabled();
    expect(screen.getByText(/Extraction agent: running/)).toBeInTheDocument();
  });

  it('allows diagnostic-only processing when no borrower mappings are available', async () => {
    const active = session();
    active.documents = [1, 2, 4].map((role) => ({ id: `d${role}`, name: `MTB_Query_${role}.xlsx`, size: 10, extension: '.xlsx', metadata: {} }));
    active.preflight = { created_at: 'now', input_fingerprint: 'unmapped', recognized_mtb: true,
      query_roles: [1, 2, 4], missing_query_roles: [], duplicate_query_roles: [], query_file_set_valid: true,
      document_count: 3, attribute_count: 1, attributes: ['Unsupported_Field__c'],
      account_count: 0, loan_count: 0, issue_count: 1, priority_issues: ['NO_EXPLICIT_LOAN_RECORDS'],
      suggested_next_step: 'Continue diagnostic-only review', comparison: 'First', external_processing: false };
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ results: [], total: 0 }), { status: 200 })));
    render(<GuidedAssistant {...props(active)} />);
    const prepare = screen.getByRole('button', { name: 'Prepare review' });
    await waitFor(() => expect(prepare).toBeEnabled());
    expect(screen.getByText(/No explicit loan numbers could be mapped/)).toBeInTheDocument();
    await userEvent.click(prepare);
    expect(screen.getByRole('group', { name: 'Confirm external processing' })).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalledWith(expect.stringContaining('/process'), expect.anything());
  });

  it('renders persisted exact evidence as escaped text with report provenance', async () => {
    const inspection: LoanInspection = { report_id: 'r1', loan_id: 'L25', topic: 'evidence', recommendation_path: 'POLICY_REVIEW_REQUIRED', final_action: 'MANUAL_REVIEW', missing_inputs: [], conflicts: [], rules: [], total_rules: 40, total_evidence: 100,
      evidence: [{ id: 'ev1', workbook: 'q1.xlsx', sheet: 'Loan', row: 2, record_id: 'L25', field: 'requested_amount', value: '<script>9007199254740993123.00</script>' }] };
    const { container } = render(<InspectionCard inspection={inspection} />);
    expect(screen.getByText('Source cells · showing 1 of 100').closest('details')).toHaveAttribute('open');
    expect(screen.getByText('<script>9007199254740993123.00</script>')).toBeVisible();
    expect(screen.getByText(/Saved report r1/)).toBeInTheDocument();
    expect(container.querySelector('script')).toBeNull();
  });

  it('offers selected-loan inspection, drafts questions without sending and records separate notes', async () => {
    const active = session();
    const loan: LoanRecommendation = { loan_id: 'L25', borrower_account_id: 'A25', decision_status: 'Human Review', currency: 'USD', mode: 'PRODUCTION',
      policy_version: 'draft', normalized_amounts_usd: {}, normalized_data: {}, rule_results: [], source_evidence: [], missing_inputs: [], conflicts: [], recommendation_path: 'POLICY_REVIEW_REQUIRED', final_action: 'MANUAL_REVIEW', human_review_required: true, explanation: 'Review', llm_error: null };
    active.status = 'completed';
    active.report = { id: 'r1', created_at: 'today', decision: 'needs_review', summary: 'Review', document_count: 1, findings: [],
      underwriting: { mode: 'PRODUCTION', currency: 'USD', policy_version: 'draft', source_document: 'LLM_Loan_Approval_Design_Guide_v4_USD_Copilot.pdf', source_available: true, data_quality_issues: [], loan_results: [loan] } };
    const p = props(active); render(<GuidedAssistant {...p} />);
    await userEvent.click(screen.getByText('Evidence tools and follow-up questions'));
    await userEvent.click(screen.getByRole('button', { name: 'Draft a question for Claude' }));
    expect(p.onAsk).toHaveBeenCalledWith('L25', expect.stringContaining('selected loan'));
    expect(screen.getByRole('status')).toHaveTextContent('Question drafted in Message Agent below');
    expect(fetch).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Why is this loan held?' }));
    expect(fetch).toHaveBeenCalledWith('/api/sessions/s1/assistant/inspect', expect.objectContaining({ body: JSON.stringify({ loan_id: 'L25', topic: 'blockers' }) }));
    await userEvent.click(screen.getByText('Review notes and acknowledgments'));
    await userEvent.type(screen.getByRole('textbox', { name: 'Review note' }), 'Awaiting verified report');
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Review record kind' }), 'acknowledgment');
    await userEvent.click(screen.getByRole('button', { name: 'Record review note' }));
    expect(fetch).toHaveBeenCalledWith('/api/sessions/s1/assistant/notes', expect.objectContaining({ body: JSON.stringify({ report_id: 'r1', loan_id: 'L25', text: 'Awaiting verified report', kind: 'acknowledgment' }) }));
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Note saved only'));
  });

  it.each([false, true])('explicitly confirms a loan-focused Claude review and handles provider failure: %s', async (failed) => {
    const active = session();
    active.status = 'completed';
    active.report = { id: 'report-1', created_at: 'today', decision: 'needs_review', summary: 'Review', document_count: 3,
      findings: [], underwriting: { mode: 'TEST', currency: 'USD', policy_version: 'v4', source_document: 'LLM_Loan_Approval_Design_Guide_v4_USD_Copilot.pdf', source_available: false, data_quality_issues: [], loan_results: [{
        loan_id: 'L1', borrower_account_id: 'A1', decision_status: 'Human Review', currency: 'USD', mode: 'TEST', policy_version: 'v4',
        normalized_amounts_usd: {}, normalized_data: {}, rule_results: [], source_evidence: [], missing_inputs: [], conflicts: [], recommendation_path: 'SIMULATED_REVIEW', final_action: 'NO_OPERATIONAL_ACTION', human_review_required: true, explanation: 'Fixed engine result', llm_error: null,
      }] } };
    const review: LoanAIReview = { id: 'review1', created_at: 'today', loan_id: 'L1', report_id: 'report-1', model: 'mock-model', context_note: 'Selected loan only; normalized context.',
      analysis: { recommended_route: 'MANUAL_REVIEW', decision_recommendation: null, confidence: null, requires_human_approval: true, loan_ids: ['L1'], explanation: '<script>untrusted commentary</script>', positive_factors: ['Explicit identity'], risk_factors: ['Conflicting inputs'], missing_information: ['Report date'], triggered_rules: [], evidence_refs: [] } };
    const updated = { ...active, loan_ai_reviews: [review] };
    review.analysis.check_reconciliation = [{
      rule_id: 'BANKRUPTCY_5_CALENDAR_YEARS_DIAGNOSTIC', evidence_state: 'MAPPED_EVIDENCE_BLOCKED',
      rationale: 'Count evidence exists, but report selection is blocked by dates.',
      next_step: 'Supply loan as-of and report dates.', evidence_refs: ['cell-count'],
    }];
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(failed ? { detail: 'Provider unavailable' } : updated), { status: failed ? 503 : 200 }));
    vi.stubGlobal('fetch', fetchMock);
    const p = props(active); const view = render(<GuidedAssistant {...p} />);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Prepare review' })).toBeNull();
    expect(screen.getByRole('status', { name: 'Saved report requires demo refresh' })).toHaveTextContent('predates the TEST demo');
    expect(screen.queryByRole('button', { name: 'Review selected loan with Claude' })).toBeNull();
    expect(screen.getByText(/Selecting a loan starts its Claude review automatically and confirms/)).toHaveTextContent('may incur charges');
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Loan to explore' }), 'L1');
    await waitFor(() => expect(fetchMock).toHaveBeenCalledExactlyOnceWith('/api/sessions/s1/assistant/loan-review', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ loan_id: 'L1', report_id: 'report-1', expected_updated_at: 'revision1', confirmed: true }),
    })));
    if (failed) {
      expect(await screen.findByRole('alert')).toHaveTextContent('Provider unavailable');
      expect(p.onSession).not.toHaveBeenCalled();
    } else {
      await waitFor(() => expect(p.onSession).toHaveBeenCalledWith(updated));
      view.rerender(<GuidedAssistant {...p} session={updated} />);
      expect(screen.getByText('<script>untrusted commentary</script>')).toBeVisible();
      expect(screen.getByText('Claude audit details · model, context and evidence').closest('details')).not.toHaveAttribute('open');
      expect(screen.getByText('Count evidence exists, but report selection is blocked by dates.')).not.toBeVisible();
      await userEvent.click(screen.getByText('Claude audit details · model, context and evidence'));
      expect(screen.getByText('Historical strict-workflow commentary — not the current TEST demo.')).toBeVisible();
      expect(screen.getByRole('region', { name: 'Claude reconciliation of unresolved checks' })).toHaveTextContent('Mapped evidence found—use is blocked');
      expect(screen.getByText('Count evidence exists, but report selection is blocked by dates.')).toBeVisible();
      expect(view.container.querySelector('script')).toBeNull();
      expect(screen.getByLabelText('Selected-loan Claude review result')).not.toHaveFocus();
      view.rerender(<GuidedAssistant {...p} session={{ ...updated, report: { ...updated.report!, id: 'different-report' } }} />);
      expect(screen.queryByLabelText('Selected-loan Claude rationale')).toBeNull();
    }
  });

  it('places the latest matching lookup next to controls without stealing focus or scrolling', async () => {
    const scroll = vi.fn();
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, writable: true, value: scroll });
    const active = session();
    const inspection: LoanInspection = { report_id: 'r1', loan_id: 'L25', topic: 'evidence', recommendation_path: 'POLICY_REVIEW_REQUIRED', final_action: 'MANUAL_REVIEW', missing_inputs: [], conflicts: [], rules: [], total_rules: 0, total_evidence: 0, evidence: [] };
    active.report = { id: 'r1', created_at: 'today', decision: 'needs_review', summary: 'Review', document_count: 1, findings: [], underwriting: { mode: 'PRODUCTION', currency: 'USD', policy_version: 'draft', source_document: 'LLM_Loan_Approval_Design_Guide_v4_USD_Copilot.pdf', source_available: true, data_quality_issues: [], loan_results: [{ loan_id: 'L25', borrower_account_id: null, decision_status: 'Human Review', currency: 'USD', mode: 'PRODUCTION', policy_version: 'draft', normalized_amounts_usd: {}, normalized_data: {}, rule_results: [], source_evidence: [], missing_inputs: [], conflicts: [], recommendation_path: 'POLICY_REVIEW_REQUIRED', final_action: 'MANUAL_REVIEW', human_review_required: true, explanation: 'Review', llm_error: null }] } };
    const p = props(active);
    const view = render(<GuidedAssistant {...p} />);
    const selector = screen.getByRole('combobox', { name: 'Loan to explore' });
    selector.focus();
    view.rerender(<GuidedAssistant {...p} session={{ ...active, messages: [{ id: 'lookup', role: 'assistant', content: 'Lookup', created_at: 'today', origin: 'local_assistant', inspection }] }} />);
    expect(selector).toHaveFocus();
    expect(scroll).not.toHaveBeenCalled();
    expect(screen.getByRole('region', { name: 'Saved evidence for loan L25' })).toBeInTheDocument();
    expect(screen.getByText('Source cells · showing 0 of 0').closest('details')).toHaveAttribute('open');
  });

  it.each([1, 2])('continues confirmed preparation into loan exploration with %s loans', async (count) => {
    const active = session();
    const loan: LoanRecommendation = { loan_id: 'L1', borrower_account_id: 'A1', decision_status: 'Human Review', currency: 'USD', mode: 'TEST', policy_version: 'v4', normalized_amounts_usd: {}, normalized_data: {}, rule_results: [], source_evidence: [], missing_inputs: [], conflicts: [], recommendation_path: 'SIMULATED_REVIEW', final_action: 'NO_OPERATIONAL_ACTION', human_review_required: true, explanation: 'Fixed', llm_error: null };
    const completed: Session = { ...active, status: 'completed', report: { id: 'prepared-report', created_at: 'today', decision: 'needs_review', summary: 'Review', document_count: 3, findings: [], underwriting: { mode: 'TEST', currency: 'USD', policy_version: 'v4', source_document: 'LLM_Loan_Approval_Design_Guide_v4_USD_Copilot.pdf', source_available: false, data_quality_issues: [], loan_results: Array.from({ length: count }, (_, i) => ({ ...loan, loan_id: `L${i + 1}` })) } } };
    const fetchMock = vi.fn(async (url: string) => new Response(JSON.stringify(url.endsWith('/assistant/process') ? { ...active, status: 'processing' } : completed), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    const p = props(active); const view = render(<GuidedAssistant {...p} />);
    await userEvent.click(screen.getByRole('button', { name: 'Prepare review' }));
    expect(screen.getByRole('group', { name: 'Confirm external processing' })).toHaveTextContent('additional charge');
    expect(fetchMock).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Confirm and process' }));
    await waitFor(() => expect(p.onSession).toHaveBeenCalled());
    view.rerender(<GuidedAssistant {...p} session={{ ...active, status: 'processing' }} disabled />);
    view.rerender(<GuidedAssistant {...p} session={completed} />);
    const selector = screen.getByRole('combobox', { name: 'Loan to explore' });
    if (count === 1) {
      await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => url.endsWith('/loan-review'))).toHaveLength(1));
      expect(selector).toHaveValue('L1');
      view.rerender(<GuidedAssistant {...p} session={{ ...completed, updated_at: 'later' }} />);
      expect(fetchMock.mock.calls.filter(([url]) => url.endsWith('/loan-review'))).toHaveLength(1);
    } else {
      expect(selector).not.toHaveFocus();
      expect(selector).toHaveValue('');
      expect(fetchMock.mock.calls.some(([url]) => url.endsWith('/loan-review'))).toBe(false);
    }
  });

  it('auto-reviews explicit loan selections without duplicate calls or automatic retries', async () => {
    const active = session();
    const loan: LoanRecommendation = { loan_id: 'L1', borrower_account_id: 'A1', decision_status: 'Human Review', currency: 'USD', mode: 'TEST', policy_version: 'v4', normalized_amounts_usd: {}, normalized_data: {}, rule_results: [], source_evidence: [], missing_inputs: [], conflicts: [], recommendation_path: 'SIMULATED_REVIEW', final_action: 'NO_OPERATIONAL_ACTION', human_review_required: true, explanation: 'Fixed', llm_error: null };
    active.status = 'completed';
    active.report = { id: 'report-auto', created_at: 'today', decision: 'needs_review', summary: 'Review', document_count: 3, findings: [], underwriting: { mode: 'TEST', currency: 'USD', policy_version: 'v4', source_document: 'LLM_Loan_Approval_Design_Guide_v4_USD_Copilot.pdf', source_available: false, data_quality_issues: [], loan_results: [loan, { ...loan, loan_id: 'L2' }] } };
    active.report.underwriting!.loan_results[1].source_evidence = [
      { id: 'score-q1', record_id: 'R1', field: 'nFUSE__Credit_Score__c', value: '780', workbook: 'Query1', sheet: 'Data', row: 2 },
      { id: 'score-q4', record_id: 'R1', field: 'nFUSE__Credit_Score__c', value: '710', workbook: 'Query4', sheet: 'Data', row: 2 },
    ];
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ detail: 'Provider unavailable' }), { status: 503 }));
    vi.stubGlobal('fetch', fetchMock);
    const p = props(active); const view = render(<GuidedAssistant {...p} />);
    const selector = screen.getByRole('combobox', { name: 'Loan to explore' });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(selector).toHaveValue('');
    expect(selector).toHaveAttribute('aria-describedby', 'loan-review-disclosure');
    await userEvent.selectOptions(selector, 'L2');
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByRole('region', { name: 'Choose credit-report evidence' })).toHaveTextContent('780');
    expect(screen.getByRole('region', { name: 'Choose credit-report evidence' })).toHaveTextContent('710');
    await userEvent.click(screen.getByRole('radio', { name: /Score 710/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Continue review with selected record' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock).toHaveBeenLastCalledWith('/api/sessions/s1/assistant/loan-review', expect.objectContaining({ body: JSON.stringify({ loan_id: 'L2', report_id: 'report-auto', expected_updated_at: 'revision1', confirmed: true, preferred_evidence_ref: 'score-q4' }) }));
    await screen.findByRole('button', { name: 'Retry Claude review' });
    view.rerender(<GuidedAssistant {...p} session={{ ...active, updated_at: 'revision2' }} />);
    await userEvent.selectOptions(selector, '');
    await userEvent.selectOptions(selector, 'L2');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    view.rerender(<GuidedAssistant {...p} session={{ ...active, report: { ...active.report!, id: 'new-report' } }} />);
    expect(selector).toHaveValue('');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});