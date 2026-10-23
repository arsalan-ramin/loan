import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import GuidedAssistant, { InspectionCard } from './GuidedAssistant';
import type { LoanInspection, LoanRecommendation, Session } from './types';

function session(): Session {
  return { id: 's1', title: 'Review', mode: 'PRODUCTION', created_at: 'today', updated_at: 'revision1',
    status: 'ready', messages: [], documents: [{ id: 'd1', name: 'evidence.txt', size: 10, extension: '.txt', metadata: {} }],
    rules: [], steps: [], report: null, error: null };
}

function props(active = session()) {
  return { session: active, disabled: false, run: async (_label: string, work: () => Promise<void>) => work(),
    onSession: vi.fn(), onUpload: vi.fn(), onReport: vi.fn(), onDownload: vi.fn(), onAsk: vi.fn() };
}

describe('Guided assistant workflow', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(session()), { status: 200 }))));

  it('runs local validation without processing or sending a chat request', async () => {
    const p = props(); render(<GuidedAssistant {...p} />);
    await userEvent.click(screen.getByRole('button', { name: 'Validate files locally' }));
    expect(fetch).toHaveBeenCalledExactlyOnceWith('/api/sessions/s1/preflight', { method: 'POST' });
    await waitFor(() => expect(p.onSession).toHaveBeenCalled());
  });

  it('requires explicit charge-disclosing confirmation and binds it to the current revision', async () => {
    const p = props(); render(<GuidedAssistant {...p} />);
    await userEvent.click(screen.getByRole('button', { name: 'Prepare review' }));
    expect(fetch).not.toHaveBeenCalled();
    const confirmation = within(screen.getByRole('group', { name: 'Confirm external processing' }));
    expect(confirmation.getByText(/may incur charges/)).toBeInTheDocument();
    await userEvent.click(confirmation.getByRole('button', { name: 'Confirm and process' }));
    expect(fetch).toHaveBeenCalledWith('/api/sessions/s1/assistant/process', expect.objectContaining({ method: 'POST', body: JSON.stringify({ confirmed: true, expected_updated_at: 'revision1' }) }));
  });

  it('requires local validation and an explicit borrower selection for MTB processing', async () => {
    const active = session();
    active.documents = [{ id: 'd1', name: 'MTB_Query_1.xlsx', size: 10, extension: '.xlsx', metadata: {} }];
    active.preflight = { created_at: 'now', input_fingerprint: 'hash', recognized_mtb: true, query_roles: [1, 2, 4],
      missing_query_roles: [], document_count: 3, loan_count: 2, account_count: 1, issue_count: 0,
      priority_issues: [], suggested_next_step: 'Select account', comparison: 'First', external_processing: false };
    const selected = { ...active, selected_account_id: 'A1' };
    const fetchMock = vi.fn(async (url: string) => {
      const body = url.includes('/accounts?')
        ? { results: [{ account_id: 'A1', name: 'Client A', loan_count: 2, loan_ids: ['MTB-L003', 'MTB-L004'] }], total: 1, offset: 0, limit: 50, account_count: 1 }
        : url.endsWith('/account') ? selected : active;
      return new Response(JSON.stringify(body), { status: 200 });
    });
    vi.stubGlobal('fetch', fetchMock);
    const p = props(active); const view = render(<GuidedAssistant {...p} />);
    await screen.findByRole('option', { name: /Client A/ });
    await userEvent.type(screen.getByRole('textbox', { name: 'Search accounts or clients' }), 'MTB-L003');
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/sessions/s1/accounts?q=MTB-L003&offset=0&limit=50', undefined));
    expect(await screen.findByRole('option', { name: /matched loan MTB-L003/ })).toBeInTheDocument();
    const prepare = screen.getByRole('button', { name: 'Prepare review' });
    expect(prepare).toBeDisabled();
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Choose account/client' }), 'A1');
    expect(fetchMock).toHaveBeenCalledWith('/api/sessions/s1/account', expect.objectContaining({ method: 'PUT', body: JSON.stringify({ account_id: 'A1' }) }));
    view.rerender(<GuidedAssistant {...p} session={selected} />);
    expect(prepare).toBeEnabled();
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

  it('shows escaped priority blockers and comparison without claiming approval', () => {
    const active = session();
    active.preflight = { created_at: 'now', input_fingerprint: 'hash', recognized_mtb: true, query_roles: [1],
      missing_query_roles: [2, 4], document_count: 1, loan_count: 0, account_count: 0, issue_count: 20,
      priority_issues: ['<script>missing borrower</script>'], suggested_next_step: 'Replace source', comparison: 'Previous issue count 30; current 20', external_processing: false };
    const { container } = render(<GuidedAssistant {...props(active)} />);
    expect(screen.getByText('<script>missing borrower</script>')).toBeInTheDocument();
    expect(screen.getByText(/first 1 of 20/)).toBeInTheDocument();
    expect(screen.getByText(/Previous issue count 30/)).toBeInTheDocument();
    expect(screen.getByText(/Missing Query roles: 2, 4/)).toBeInTheDocument();
    expect(container.querySelector('script')).toBeNull();
  });

  it('disables local actions during processing and shows step progress', () => {
    const active = { ...session(), status: 'processing' as const, steps: [{ agent: 'Extraction agent', status: 'running' as const, detail: 'Reading files' }] };
    render(<GuidedAssistant {...props(active)} disabled />);
    expect(screen.getByRole('button', { name: 'Validate files locally' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Prepare review' })).toBeDisabled();
    expect(screen.getByText(/Extraction agent: running/)).toBeInTheDocument();
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
    const loan: LoanRecommendation = { loan_id: 'L25', borrower_account_id: 'A25', currency: 'USD', mode: 'PRODUCTION',
      policy_version: 'draft', normalized_amounts_usd: {}, normalized_data: {}, rule_results: [], source_evidence: [], missing_inputs: [], conflicts: [], recommendation_path: 'POLICY_REVIEW_REQUIRED', final_action: 'MANUAL_REVIEW', human_review_required: true, explanation: 'Review', llm_error: null };
    active.status = 'completed';
    active.report = { id: 'r1', created_at: 'today', decision: 'needs_review', summary: 'Review', document_count: 1, findings: [],
      underwriting: { mode: 'PRODUCTION', currency: 'USD', policy_version: 'draft', source_document: 'LLM_Loan_Approval_Design_Guide_v4_USD_Copilot.pdf', source_available: true, data_quality_issues: [], loan_results: [loan] } };
    const p = props(active); render(<GuidedAssistant {...p} />);
    await userEvent.click(screen.getByRole('button', { name: 'Draft a question for Claude' }));
    expect(p.onAsk).toHaveBeenCalledWith('L25', expect.stringContaining('selected loan'));
    expect(screen.getByRole('status')).toHaveTextContent('Question drafted in Message Agent below');
    expect(fetch).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Why is this loan held?' }));
    expect(fetch).toHaveBeenCalledWith('/api/sessions/s1/assistant/inspect', expect.objectContaining({ body: JSON.stringify({ loan_id: 'L25', topic: 'blockers' }) }));
    await userEvent.type(screen.getByRole('textbox', { name: 'Review note' }), 'Awaiting verified report');
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Review record kind' }), 'acknowledgment');
    await userEvent.click(screen.getByRole('button', { name: 'Record review note' }));
    expect(fetch).toHaveBeenCalledWith('/api/sessions/s1/assistant/notes', expect.objectContaining({ body: JSON.stringify({ report_id: 'r1', loan_id: 'L25', text: 'Awaiting verified report', kind: 'acknowledgment' }) }));
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Note saved only'));
  });

  it('places the latest matching lookup next to controls and focuses the result instead of the chat bottom', async () => {
    const active = session();
    const inspection: LoanInspection = { report_id: 'r1', loan_id: 'L25', topic: 'evidence', recommendation_path: 'POLICY_REVIEW_REQUIRED', final_action: 'MANUAL_REVIEW', missing_inputs: [], conflicts: [], rules: [], total_rules: 0, total_evidence: 0, evidence: [] };
    active.report = { id: 'r1', created_at: 'today', decision: 'needs_review', summary: 'Review', document_count: 1, findings: [], underwriting: { mode: 'PRODUCTION', currency: 'USD', policy_version: 'draft', source_document: 'LLM_Loan_Approval_Design_Guide_v4_USD_Copilot.pdf', source_available: true, data_quality_issues: [], loan_results: [{ loan_id: 'L25', borrower_account_id: null, currency: 'USD', mode: 'PRODUCTION', policy_version: 'draft', normalized_amounts_usd: {}, normalized_data: {}, rule_results: [], source_evidence: [], missing_inputs: [], conflicts: [], recommendation_path: 'POLICY_REVIEW_REQUIRED', final_action: 'MANUAL_REVIEW', human_review_required: true, explanation: 'Review', llm_error: null }] } };
    const p = props(active);
    const view = render(<GuidedAssistant {...p} />);
    view.rerender(<GuidedAssistant {...p} session={{ ...active, messages: [{ id: 'lookup', role: 'assistant', content: 'Lookup', created_at: 'today', origin: 'local_assistant', inspection }] }} />);
    expect(screen.getByLabelText('Latest loan lookup result')).toHaveFocus();
    expect(screen.getByRole('region', { name: 'Saved evidence for loan L25' })).toBeInTheDocument();
    expect(screen.getByText('Source cells · showing 0 of 0').closest('details')).toHaveAttribute('open');
  });
});