import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App from './App';
import type { Session, UnderwritingBatch } from './types';

const batch: UnderwritingBatch = {
  mode: 'PRODUCTION', currency: 'USD', policy_version: 'v4', source_document: 'LLM_Loan_Approval_Design_Guide_v4_USD_Copilot.pdf',
  source_available: false, data_quality_issues: ['No explicit loan records found.'], loan_results: [],
};
function session(overrides: Partial<Session> = {}): Session {
  return {
    id: 'v4-session', title: 'V4 review', created_at: '2026-10-20T12:00:00Z', updated_at: '2026-10-20T12:00:00Z',
    status: 'completed', messages: [], documents: [], rules: [], steps: [], error: null,
    report: { id: 'report-v4', created_at: '2026-10-20T12:00:00Z', decision: 'needs_review', summary: 'Human review required.', document_count: 3, findings: [], underwriting: batch },
    ...overrides,
  };
}
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

async function open(active: Session, handler?: (url: string, init?: RequestInit) => Response | Promise<Response> | undefined) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const handled = handler?.(url, init);
    if (handled !== undefined) return handled;
    if (url.endsWith('/health')) return response({ status: 'ok' });
    return response(url === '/api/sessions' ? [active] : active);
  });
  vi.stubGlobal('fetch', fetchMock);
  const user = userEvent.setup();
  render(<App />);
  await user.click(await screen.findByRole('button', { name: /V4 review/ }));
  await waitFor(() => expect(screen.getByRole('heading', { name: 'V4 review' })).toBeInTheDocument());
  return { user, fetchMock };
}

describe('V4 app integration', () => {
  it.each([false, true])('shows Claude output after Process without opening the report (provider failure: %s)', async (failed) => {
    const scroll = vi.fn();
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, writable: true, value: scroll });
    const active = session({ status: 'ready', report: null, documents: [
      { id: 'd', name: 'evidence.txt', extension: '.txt', size: 10, metadata: {} },
    ] });
    const completed = session({ documents: active.documents });
    completed.report!.llm_analysis = failed ? null : 'Review the supplied evidence; missing mappings remain unknown.';
    completed.report!.llm_error = failed ? 'Anthropic unavailable; deterministic report retained.' : null;
    let started = false;
    const { user, fetchMock } = await open(active, (url) => {
      if (url.endsWith('/process')) {
        started = true;
        return response({ ...active, status: 'processing' }, 202);
      }
      if (started && url.endsWith('/v4-session')) return response(completed);
      return undefined;
    });
    await user.click(screen.getByRole('button', { name: 'Process documents' }));
    expect(screen.getByText(/Claude’s explanation will appear here/)).toBeInTheDocument();
    const text = failed ? completed.report!.llm_error! : completed.report!.llm_analysis!;
    expect(await screen.findByText(text, {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Aggregate report commentary' })).toBeVisible();
    expect(screen.getByText('Aggregate report commentary').closest('details')).toBeNull();
    expect(screen.getByLabelText('Processing explanation result')).not.toHaveFocus();
    expect(scroll).not.toHaveBeenCalled();
    expect(screen.getByText(text)).toBeVisible();
    expect(screen.queryByRole('heading', { name: 'Per-loan underwriting report' })).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([url]) => url.endsWith('/process'))).toHaveLength(1);
    expect(fetchMock.mock.calls.some(([url]) => url.endsWith('/messages'))).toBe(false);
  });

  it('recognizes underwriting-only reports, defaults missing mode to TEST and prefers v4 over the legacy adapter', async () => {
    const active = session({ mode: undefined });
    active.report!.policy_evaluation = {
      policy_version: 'draft-v1', source_document: 'legacy.pdf', source_sha256: null, policy_status: 'draft', recommended_route: 'LEGACY_ROUTE',
      decision_recommendation: null, requires_human_approval: true, missing_information: [], rule_results: [], profiles: [], limitations: [],
    };
    const { user } = await open(active);
    expect(screen.getByRole('combobox', { name: 'Review mode' })).toHaveValue('TEST');
    await user.click(screen.getByRole('button', { name: /^Review report/ }));
    expect(screen.getByRole('heading', { name: 'Per-loan underwriting report' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Draft underwriting route' })).not.toBeInTheDocument();
    expect(screen.queryByText('LEGACY_ROUTE')).not.toBeInTheDocument();
    expect(screen.queryByText('Underwriting evaluation missing')).not.toBeInTheDocument();
    for (const label of ['Download HTML', 'Download JSON', 'Download CSV']) expect(screen.getByRole('button', { name: label })).toBeEnabled();
    expect(screen.getByText('0 explicit loan records')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /^approved$/i })).not.toBeInTheDocument();
  });

  it('sends mode only, disables edits while pending and replaces the report with the returned invalidated session', async () => {
    let finish!: (value: Response) => void;
    const active = session({ mode: 'PRODUCTION' });
    const { user, fetchMock } = await open(active, (url) => url.endsWith('/mode') ? new Promise<Response>((resolve) => { finish = resolve; }) : undefined);
    await user.click(screen.getByRole('button', { name: /^Review report/ }));
    const selector = screen.getByRole('combobox', { name: 'Review mode' });
    await user.selectOptions(selector, 'TEST');
    expect(selector).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Download CSV' })).toBeDisabled();
    expect(fetchMock).toHaveBeenCalledWith('/api/sessions/v4-session/mode', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: 'TEST' }),
    });
    finish(response(session({ mode: 'TEST', status: 'ready', report: null, documents: [
      { id: 'mtb', name: 'MTB_Query_1.xlsx', size: 10, extension: '.xlsx', metadata: {} },
    ] })));
    await waitFor(() => expect(selector).toHaveValue('TEST'));
    expect(selector).toBeEnabled();
    expect(screen.queryByRole('heading', { name: 'Per-loan underwriting report' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Download CSV' })).not.toBeInTheDocument();
    expect(screen.getByText('A good review starts with your documents.')).toBeInTheDocument();
    expect(screen.getByText(/Changing mode clears the current report; process documents again./)).toBeInTheDocument();
  });

  it('preserves the prior mode and report on mode update conflict', async () => {
    const { user } = await open(session({ mode: 'PRODUCTION' }), (url) => url.endsWith('/mode') ? response({ detail: 'Wait for the current review to finish.' }, 409) : undefined);
    await user.selectOptions(screen.getByRole('combobox', { name: 'Review mode' }), 'TEST');
    expect(await screen.findByRole('alert')).toHaveTextContent('Wait for the current review to finish.');
    expect(screen.getByRole('combobox', { name: 'Review mode' })).toHaveValue('PRODUCTION');
    expect(screen.getByRole('combobox', { name: 'Review mode' })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: /^Review report/ }));
    expect(screen.getByRole('heading', { name: 'Per-loan underwriting report' })).toBeInTheDocument();
  });

  it('disables mode changes while processing an MTB session', async () => {
    await open(session({ status: 'processing', report: null, mode: 'TEST', documents: [{ id: 'd', name: 'MTB_Query_1_Sample_Data.xlsx', size: 10, extension: '.xlsx', metadata: {} }] }));
    expect(screen.getByRole('combobox', { name: 'Review mode' })).toHaveValue('TEST');
    expect(screen.getByRole('combobox', { name: 'Review mode' })).toBeDisabled();
  });

  it.each(['JSON', 'CSV'])('shows %s export failures, re-enables retry and keeps the deterministic report', async (format) => {
    let finish!: (value: Response) => void;
    const { user, fetchMock } = await open(session(), (url) => url.includes('/underwriting?') ? new Promise<Response>((resolve) => { finish = resolve; }) : undefined);
    await user.click(screen.getByRole('button', { name: /^Review report/ }));
    const button = screen.getByRole('button', { name: `Download ${format}` });
    await user.click(button);
    expect(button).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Download HTML' })).toBeDisabled();
    expect(screen.getByRole('combobox', { name: 'Review mode' })).toBeDisabled();
    expect(fetchMock).toHaveBeenCalledWith(`/api/sessions/v4-session/underwriting?format=${format.toLowerCase()}`);
    finish(response({ detail: 'Complete an underwriting review before downloading results.' }, 409));
    expect(await screen.findByRole('alert')).toHaveTextContent('Complete an underwriting review before downloading results.');
    expect(button).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Download HTML' })).toBeEnabled();
    expect(screen.getByRole('heading', { name: 'Per-loan underwriting report' })).toBeInTheDocument();
    await user.click(button);
    finish(response({ detail: 'Temporary export failure.' }, 503));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Temporary export failure.'));
  });

  it('does not add MTB controls or exports to generic legacy reports', async () => {
    const active = session();
    active.report!.underwriting = null;
    const { user } = await open(active);
    expect(screen.queryByRole('combobox', { name: 'Review mode' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^Review report/ }));
    expect(screen.getByRole('button', { name: 'Download HTML' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Download JSON' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Download CSV' })).not.toBeInTheDocument();
  });
});