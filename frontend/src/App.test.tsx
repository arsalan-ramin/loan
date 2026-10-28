import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import type { Session } from './types';

const session = (overrides: Partial<Session> = {}): Session => ({
  id: 'session-1', title: 'Untitled session', created_at: '2026-10-20T12:00:00Z', updated_at: '2026-10-20T12:00:00Z', status: 'awaiting_upload', messages: [], documents: [], rules: [], steps: [], report: null, error: null, ...overrides,
});
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

describe('Agent', () => {
  it('shows follow-up failure beside the composer, preserves the question and shows the retry reply', async () => {
    const scroll = vi.fn();
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, writable: true, value: scroll });
    const active = session({ status: 'ready', documents: [{ id: 'd1', name: 'notes.txt', size: 10, extension: '.txt', metadata: {} }] });
    let fail = true;
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url.endsWith('/health')) return response({ status: 'ok' });
      if (url === '/api/sessions') return response([active]);
      if (url.endsWith('/messages')) {
        if (fail) return response({ detail: 'Claude reached the 2,048-token output limit.' }, 502);
        return response({ ...active, messages: [
          { id: 'q', role: 'user', content: 'What is missing?', created_at: active.created_at },
          { id: 'a', role: 'assistant', content: 'A dated source report is missing.', created_at: active.created_at },
        ] });
      }
      return response(active);
    }));
    const user = userEvent.setup(); render(<App />);
    await user.click(await screen.findByRole('button', { name: /Untitled session/ }));
    const input = screen.getByRole('textbox', { name: 'Message Agent' });
    await user.type(input, 'What is missing?');
    await user.click(screen.getByRole('button', { name: 'Send message' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Your follow-up could not be answered');
    expect(screen.getByRole('alert')).toHaveTextContent('2,048-token output limit');
    expect(input).toHaveValue('What is missing?');
    expect(screen.getByRole('alert').closest('.chat-feedback')).not.toBeNull();
    fail = false;
    await user.click(screen.getByRole('button', { name: 'Send message' }));
    const reply = await screen.findByRole('region', { name: 'Latest agent reply' });
    expect(reply).toHaveTextContent('A dated source report is missing.');
    expect(screen.queryByRole('alert')).toBeNull();
    expect(input).toHaveValue('');
    expect(scroll).not.toHaveBeenCalled();
  });

  it('renders requested assistant Markdown tables as safe semantic tables', async () => {
    const active = session({ status: 'ready' });
    const markdown = '| Attribute | Value |\n| --- | --- |\n| Product | Express BALOC |\n| Note | &lt;script&gt;nope&lt;/script&gt; |';
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url.endsWith('/health')) return response({ status: 'ok' });
      if (url === '/api/sessions') return response([active]);
      if (url.endsWith('/messages')) return response({ ...active, messages: [
        { id: 'q', role: 'user', content: 'List all loan attributes in a table', created_at: active.created_at },
        { id: 'a', role: 'assistant', content: markdown, created_at: active.created_at, origin: 'local_assistant' },
      ] });
      return response(active);
    }));
    const user = userEvent.setup(); render(<App />);
    await user.click(await screen.findByRole('button', { name: /Untitled session/ }));
    await user.type(screen.getByRole('textbox', { name: 'Message Agent' }), 'List all loan attributes in a table');
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    const table = await screen.findByRole('table');
    expect(within(table).getByRole('columnheader', { name: 'Attribute' })).toBeInTheDocument();
    expect(within(table).getByRole('cell', { name: 'Express BALOC' })).toBeInTheDocument();
    expect(table.querySelector('script')).toBeNull();
    expect(table).toHaveTextContent('<script>nope</script>');
  });

  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => url.endsWith('/health') ? response({ status: 'ok' }) : response([])));
  });

  it('provides upload-first guidance without inventing documents or sessions', async () => {
    render(<App />);
    expect(await screen.findByText('Your documents are the starting point.')).toBeInTheDocument();
    expect(await screen.findByText(/Your sessions will live here/)).toBeInTheDocument();
    expect(screen.getByText('Start by adding your first document below.')).toBeInTheDocument();
    expect(screen.getByText(/Chat context and document evidence are sent to Anthropic/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /^agent\s*\.$/ })).toBeInTheDocument();
    expect(screen.getByText('Agent assistant')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Message Agent' })).toBeEnabled();
    expect(screen.getByRole('textbox', { name: 'Message Agent' })).toHaveAttribute('placeholder', 'Ask a question, or upload your documents to begin…');
    expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Process documents' })).toBeDisabled();
    expect(screen.getByText('No documents added yet')).toBeInTheDocument();
    expect(screen.getByLabelText('Choose documents')).toHaveAttribute('accept', '.pdf,.docx,.xlsx,.txt,.md,.csv,.json');
    expect(screen.getByText('XLSX up to 50 MB · Other files up to 20 MB · 20 files per session')).toBeInTheDocument();
    expect(screen.getByText(/.xls and .xlsm are unsupported/)).toBeInTheDocument();
    expect(screen.getByText(/Upload the three MTB source workbooks/)).toBeInTheDocument();
    expect(screen.getByText(/.xls and .xlsm are unsupported/)).toBeInTheDocument();
    expect(screen.getByText(/V4 guideline available\. Amounts use actual USD dollars/)).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalledWith('/api/sessions', expect.objectContaining({ method: 'POST' }));
  });

  it('uploads one XLSX and displays all sheet metadata, zero data rows and escaped warnings', async () => {
    const warning = 'Schema only. <img src=x onerror=alert(1)>';
    const uploaded = session({ status: 'ready', documents: [{
      id: 'excel', name: 'schema.xlsx', extension: '.xlsx', size: 100, metadata: {
        data_row_count: 0, warning, sheets: [
          { name: '<b>Loans</b>', headers: ['<script>alert(1)</script>', 'Amount'], header_row: 3, data_rows: 0, columns: 2, is_reference: false },
          { name: 'README', headers: [], header_row: null, data_rows: 8, columns: 1, is_reference: true },
        ],
      },
    }] });
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/health')) return response({ status: 'ok' });
      if (url.endsWith('/documents')) return response(uploaded);
      if (url.endsWith('/preflight')) return response(uploaded);
      return response(init?.method === 'POST' ? session() : []);
    }));
    const user = userEvent.setup(); const { container } = render(<App />);
    await screen.findByText(/Your sessions will live here/);
    await user.upload(screen.getByLabelText('Choose documents'), new File(['excel'], 'schema.xlsx', { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }));
    const schema = within((await screen.findByText('schema.xlsx')).closest('li')!);
    expect(schema.getByText('Data rows (excluding README sheets): 0')).toBeInTheDocument();
    expect(schema.getByText('Sheet: <b>Loans</b>')).toBeInTheDocument();
    expect(schema.getByText('2 columns · 0 data rows · Header row: 3')).toBeInTheDocument();
    expect(schema.getByText('Headers: <script>alert(1)</script>, Amount')).toBeInTheDocument();
    expect(schema.getByText('Sheet: README (reference)')).toBeInTheDocument();
    expect(schema.getByText('1 columns · 8 data rows · Header row: None')).toBeInTheDocument();
    expect(schema.getByText(warning)).toBeInTheDocument();
    expect(container.querySelector('script, img, .excel-sheet b')).toBeNull();
    expect(screen.getByRole('button', { name: 'Process documents' })).toBeDisabled();
  });

  it('accepts the three MTB source workbooks and rejects a fourth workbook', async () => {
    let active = session();
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/health')) return response({ status: 'ok' });
      if (url === '/api/sessions' && init?.method !== 'POST') return response([]);
      if (url.endsWith('/documents')) {
        const form = init?.body as FormData;
        const file = form.get('files') as File;
        active = { ...active, status: 'ready', documents: [...active.documents, {
          id: `doc-${active.documents.length + 1}`, name: file.name, extension: '.xlsx', size: file.size, metadata: {},
        }] };
        return response(active);
      }
      return response(active);
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup(); render(<App />);
    await screen.findByText(/Your sessions will live here/);
    for (const name of ['MTB_Query_1.xlsx', 'MTB_Query_2.xlsx', 'MTB_Query_4.xlsx']) {
      if (active.documents.length) await user.click(screen.getByRole('button', { name: 'Add documents' }));
      await user.upload(screen.getByLabelText('Choose documents'), new File([name], name, {
        type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      }));
      expect(await screen.findByText(name)).toBeInTheDocument();
    }
    expect(active.documents).toHaveLength(3);
    expect(fetchMock.mock.calls.filter(([url]) => url.endsWith('/preflight'))).toHaveLength(3);
    expect(fetchMock.mock.calls.some(([url]) => url.endsWith('/process') || url.endsWith('/assistant/loan-review'))).toBe(false);
    await user.click(screen.getByRole('button', { name: 'Add documents' }));
    await user.upload(screen.getByLabelText('Choose documents'), new File(['extra'], 'extra.xlsx', {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    }));
    expect(await screen.findByRole('alert')).toHaveTextContent('An MTB review uses exactly three XLSX source files: Query 1, Query 2 and Query 4.');
    expect(active.documents).toHaveLength(3);
  });

  it('creates a session and sends before upload while keeping processing disabled', async () => {
    const active = session();
    const content = 'How do I begin?';
    const reply = 'Please upload your documents to begin a review.';
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/health')) return response({ status: 'ok' });
      if (url.endsWith('/messages')) return response({ ...active, messages: [
        { id: 'msg-1', role: 'user', content, created_at: active.created_at },
        { id: 'msg-2', role: 'assistant', content: reply, created_at: active.created_at },
      ] });
      if (url === '/api/sessions' && init?.method === 'POST') return response(active);
      return response([]);
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/Your sessions will live here/);
    const input = screen.getByRole('textbox', { name: 'Message Agent' });
    const sendButton = screen.getByRole('button', { name: 'Send message' });
    const processButton = screen.getByRole('button', { name: 'Process documents' });
    await user.type(input, '   ');
    expect(sendButton).toBeDisabled();
    await user.clear(input);
    await user.type(input, content);
    expect(sendButton).toBeEnabled();
    expect(processButton).toBeDisabled();
    await user.click(sendButton);
    // The reply moves from history to the dedicated latest-reply panel;
    // query the committed render rather than retaining the replaced node.
    await waitFor(() => expect(screen.getByText(reply)).toBeInTheDocument());
    expect(screen.getByText(content)).toBeInTheDocument();
    expect(input).toBeEnabled();
    expect(input).toHaveValue('');
    expect(sendButton).toBeDisabled();
    expect(processButton).toBeDisabled();
    expect(screen.getByText('No documents added yet')).toBeInTheDocument();
    const posts = fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST');
    expect(posts.map(([url]) => url)).toEqual(['/api/sessions', '/api/sessions/session-1/messages']);
    expect(fetchMock).toHaveBeenCalledWith('/api/sessions/session-1/messages', expect.objectContaining({ method: 'POST', body: JSON.stringify({ content }) }));
  });

  it('surfaces backend errors and lets the user dismiss them', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => url.endsWith('/health') ? response({ status: 'ok' }) : response({ detail: 'Session service is unavailable.' }, 503)));
    render(<App />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Session service is unavailable.');
    await userEvent.click(screen.getByRole('button', { name: 'Dismiss error' }));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Upload documents' })).toBeInTheDocument();
  });

  it('handles network failures gracefully and reconnects', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Unable to reach Agent');
    expect(screen.getByText('API offline')).toBeInTheDocument();
    fetchMock.mockImplementation(async (url: string) => url.endsWith('/health') ? response({ status: 'ok' }) : response([]));
    await userEvent.click(screen.getByRole('button', { name: 'Reconnect' }));
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    expect(screen.getByText('Connected to your API')).toBeInTheDocument();
  });

  it('creates a session on first upload, uploads multipart files, and sends chat with Enter', async () => {
    const uploaded = session({ status: 'ready', documents: [{ id: 'doc-1', name: 'contract.txt', extension: '.txt', size: 16, metadata: {} }] });
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/health')) return response({ status: 'ok' });
      if (url.endsWith('/documents')) return response(uploaded);
      if (url.endsWith('/preflight')) return response(uploaded);
      if (url.endsWith('/messages')) return response({ ...uploaded, messages: [{ id: 'msg-1', role: 'user', content: 'Check the terms', created_at: '2026-10-20T12:00:00Z' }] });
      if (init?.method === 'POST') return response(session());
      return response([]);
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/Your sessions will live here/);
    await user.upload(screen.getByLabelText('Choose documents'), new File(['An effective date'], 'contract.txt', { type: 'text/plain' }));
    expect(await screen.findByText('contract.txt')).toBeInTheDocument();
    const uploadCall = fetchMock.mock.calls.find(([url]) => url.endsWith('/documents'));
    expect(uploadCall?.[1]?.body).toBeInstanceOf(FormData);
    expect((uploadCall?.[1]?.body as FormData).getAll('files')).toHaveLength(1);
    const input = screen.getByRole('textbox', { name: 'Message Agent' });
    expect(input).toHaveAttribute('placeholder', 'Ask about documents, rules, or earlier messages…');
    await user.type(input, 'Check the terms{Enter}');
    expect(await screen.findByText('Check the terms')).toBeInTheDocument();
    expect(input).toHaveValue('');
    expect(fetchMock).toHaveBeenCalledWith('/api/sessions/session-1/messages', expect.objectContaining({ method: 'POST', body: JSON.stringify({ content: 'Check the terms' }) }));
  });

  it('preserves a message draft on API failure and clears the error after a successful retry', async () => {
    const active = session({ status: 'ready', title: 'Contract review', documents: [{ id: 'd', name: 'contract.txt', size: 10, extension: 'txt', metadata: {} }] });
    let fail = true;
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url.endsWith('/health')) return response({ status: 'ok' });
      if (url.endsWith('/messages')) {
        if (fail) return response({ detail: 'Please retry your message.' }, 500);
        return response({ ...active, messages: [{ id: 'm', role: 'user', content: 'Hello', created_at: active.created_at }] });
      }
      return response(url.endsWith('/sessions') ? [active] : active);
    }));
    const user = userEvent.setup(); render(<App />);
    await user.click(await screen.findByRole('button', { name: /Contract review/ }));
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message Agent' })).toBeEnabled());
    await user.type(screen.getByRole('textbox', { name: 'Message Agent' }), 'Hello{Enter}');
    expect(await screen.findByRole('alert')).toHaveTextContent('Please retry your message.');
    expect(screen.getByRole('textbox', { name: 'Message Agent' })).toHaveValue('Hello');
    fail = false;
    await user.click(screen.getByRole('button', { name: 'Send message' }));
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    expect(await screen.findByText('Hello')).toBeInTheDocument();
  });

  it('shows a real empty report state instead of sample findings', async () => {
    render(<App />);
    await userEvent.click(screen.getByRole('button', { name: 'Review report' }));
    const main = screen.getByRole('main');
    expect(within(main).getByText('A good review starts with your documents.')).toBeInTheDocument();
    expect(screen.queryByText('Download HTML')).not.toBeInTheDocument();
  });

  it.each([
    { label: 'legacy omitted fields', fields: {} },
    { label: 'explicit null fields', fields: { llm_analysis: null, llm_model: null, llm_error: null } },
    { label: 'explanation', fields: { llm_analysis: '<img src=x onerror=alert(1)>\nReview the evidence.', llm_model: 'claude-sonnet-5-5', llm_error: null } },
    { label: 'provider outage', fields: { llm_analysis: null, llm_model: null, llm_error: 'Anthropic is unavailable. Retry later. <script>alert(1)</script>' } },
    { label: 'explanation and warning', fields: { llm_analysis: 'Commentary cannot override a failed check.', llm_model: '<b>custom-model</b>', llm_error: 'Rate limit reached; retry later.' } },
  ])('renders $label without changing deterministic findings', async ({ fields }) => {
    const active = session({ title: 'Claude review', status: 'completed', report: {
      id: 'report-1', created_at: '2026-10-20T12:00:00Z', decision: 'rejected', summary: 'A required phrase is missing.', document_count: 1,
      findings: [{ rule_id: 'r-1', rule_name: 'Required phrase', status: 'fail', detail: 'Phrase not present.', document: 'terms.txt', evidence: 'Original document evidence' }],
      ...fields,
    } });
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url.endsWith('/health')) return response({ status: 'ok' });
      return response(url.endsWith('/sessions') ? [active] : active);
    }));
    const user = userEvent.setup();
    const { container } = render(<App />);
    await user.click(await screen.findByRole('button', { name: /Claude review/ }));
    await screen.findByText('Your review is ready.');
    await user.click(screen.getByRole('button', { name: 'View report' }));
    expect(screen.getByRole('heading', { name: /rejected/i })).toBeInTheDocument();
    expect(screen.getByText('Phrase not present.')).toBeInTheDocument();
    expect(screen.getByText('Original document evidence')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Download HTML' })).toBeEnabled();
    if (fields.llm_analysis || fields.llm_error) {
      const card = screen.getByRole('region', { name: 'Claude explanation' });
      expect(within(card).getByText(/deterministic decision and rule findings above remain authoritative/)).toBeInTheDocument();
      if (fields.llm_analysis) {
        for (const bullet of fields.llm_analysis.split('\n')) {
          expect(within(card).getByText(bullet, { exact: true })).toBeInTheDocument();
        }
        expect(within(card).getAllByRole('listitem').length).toBeGreaterThan(0);
      }
      if (fields.llm_model) expect(within(card).getByText(`Model: ${fields.llm_model}`)).toBeInTheDocument();
      if (fields.llm_error) {
        expect(within(card).getByRole('alert')).toHaveTextContent(fields.llm_error);
        expect(within(card).getByText(/report and findings remain available/)).toBeInTheDocument();
      } else expect(within(card).queryByRole('alert')).not.toBeInTheDocument();
    } else {
      expect(screen.queryByRole('region', { name: 'Claude explanation' })).not.toBeInTheDocument();
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    }
    expect(container.querySelector('script, img, .claude-model b')).toBeNull();
  });

  it.each(['MANUAL_REVIEW', 'COMPLIANCE_HOLD', 'FULL_UNDERWRITING', 'PRODUCT_INELIGIBLE', 'ELIGIBLE_FOR_SCORING'])('renders draft route %s with unknowns, provenance and no financing decision', async (route) => {
    const hash = route === 'MANUAL_REVIEW' ? null : 'a'.repeat(64);
    const active = session({ title: 'Draft route review', status: 'completed', report: {
      id: 'draft-report', created_at: '2026-10-20T12:00:00Z', decision: 'approved', summary: 'Approved financing.', document_count: 3,
      findings: [], llm_analysis: 'Commentary only.',
      policy_evaluation: {
        policy_version: 'draft-v1', source_document: 'LLM_Loan_Approval_Design_Guide_Updated.pdf', source_sha256: hash,
        policy_status: 'draft', recommended_route: route, decision_recommendation: null, requires_human_approval: true,
        missing_information: ['Currency units require confirmation.'],
        rule_results: [
          { rule_id: 'draft-unknown', status: 'unknown', route: null, page: 2, detail: 'Threshold mapping is unconfirmed.', evidence: ['<img src=x onerror=alert(1)>'] },
          { rule_id: 'draft-triggered', status: 'triggered', route, page: 3, detail: 'Draft route requires review.', evidence: ['Workbook cell A2'] },
          { rule_id: 'draft-not-triggered', status: 'not_triggered', route: null, page: 4, detail: 'Condition not met.', evidence: [] },
        ],
        profiles: [{ profile_id: 'profile-1', route: 'FULL_UNDERWRITING', missing_information: ['Income evidence missing.'], evidence: ['Workbook row 2'] }],
        limitations: ['Four-page draft; policy-owner signoff required.'],
      },
    } });
    vi.stubGlobal('fetch', vi.fn(async (url: string) => url.endsWith('/health') ? response({ status: 'ok' }) : response(url.endsWith('/sessions') ? [active] : active)));
    const user = userEvent.setup();
    const { container } = render(<App />);
    await user.click(await screen.findByRole('button', { name: /Draft route review/ }));
    await user.click(await screen.findByRole('button', { name: 'View report' }));
    const card = screen.getByRole('region', { name: 'Draft underwriting route' });
    expect(within(card).getByText(route, { exact: true })).toBeInTheDocument();
    expect(within(card).getByText('Not a lending approval or denial. Authorized review required.')).toBeInTheDocument();
    expect(within(card).getByText('LLM_Loan_Approval_Design_Guide_Updated.pdf')).toBeInTheDocument();
    expect(within(card).getByText('draft-v1')).toBeInTheDocument();
    expect(within(card).getByText(hash ?? 'Not provided')).toBeInTheDocument();
    expect(within(card).getByText('Status: Unknown')).toBeInTheDocument();
    expect(within(card).getByText('Status: Triggered')).toBeInTheDocument();
    expect(within(card).getByText('Status: Not triggered')).toBeInTheDocument();
    expect(within(card).getByText('draft-unknown · PDF page 2')).toBeInTheDocument();
    expect(within(card).getByText('Threshold mapping is unconfirmed.')).toBeInTheDocument();
    expect(within(card).getByText('Currency units require confirmation.')).toBeInTheDocument();
    expect(within(card).getByText('Income evidence missing.')).toBeInTheDocument();
    expect(within(card).getByText('Workbook row 2')).toBeInTheDocument();
    expect(within(card).getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument();
    expect(within(card).getByText('Four-page draft; policy-owner signoff required.')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Document checks' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /approved|rejected/i })).not.toBeInTheDocument();
    expect(container.querySelector('.decision-banner')).not.toHaveTextContent('Approved financing.');
    expect(container.querySelector('.decision-banner')).not.toHaveClass('approved');
    expect(container.querySelector('img, script')).toBeNull();
    expect(within(screen.getByRole('region', { name: 'Claude explanation' })).getByText(/Draft routes and document checks are not lending decisions/)).toBeInTheDocument();
  });

  it.each([undefined, null])('preserves legacy report decisions when policy evaluation is %s', async (evaluation) => {
    const active = session({ title: 'Legacy review', status: 'completed', report: {
      id: 'legacy-report', created_at: '2026-10-20T12:00:00Z', decision: 'rejected', summary: 'Legacy checks failed.', document_count: 1,
      findings: [], policy_evaluation: evaluation,
    } });
    vi.stubGlobal('fetch', vi.fn(async (url: string) => url.endsWith('/health') ? response({ status: 'ok' }) : response(url.endsWith('/sessions') ? [active] : active)));
    const user = userEvent.setup(); render(<App />);
    await user.click(await screen.findByRole('button', { name: /Legacy review/ }));
    await user.click(await screen.findByRole('button', { name: 'View report' }));
    expect(screen.getByRole('heading', { name: /rejected/i })).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Draft underwriting route' })).not.toBeInTheDocument();
  });

  it('saves editable rules using the exact API contract', async () => {
    const active = session({ title: 'Rule review' });
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/health')) return response({ status: 'ok' });
      if (url.endsWith('/rules')) return response({ ...active, rules: JSON.parse(init!.body as string).rules });
      return response(url.endsWith('/sessions') && init?.method !== 'POST' ? [active] : active);
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup(); render(<App />);
    await screen.findByRole('button', { name: /Rule review/ });
    await user.click(screen.getByRole('button', { name: 'Configure rules' }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Add a rule' }));
    await user.type(within(dialog).getByLabelText('Rule name'), 'Effective date');
    await user.type(within(dialog).getByLabelText('Text to match'), 'effective date');
    await user.selectOptions(within(dialog).getByLabelText('If not met'), 'fail');
    await user.click(within(dialog).getByRole('button', { name: 'Save rules' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByText('1 active rule')).toBeInTheDocument();
    const call = fetchMock.mock.calls.find(([url]) => url.endsWith('/rules'));
    expect(call?.[1]?.method).toBe('PUT');
    expect(JSON.parse(call?.[1]?.body as string)).toEqual({ rules: [{ id: expect.any(String), name: 'Effective date', kind: 'required_text', value: 'effective date', severity: 'fail', enabled: true }] });
  });

  it('removes uploaded documents through the backend', async () => {
    const active = session({ title: 'Remove review', status: 'ready', documents: [{ id: 'doc-1', name: 'remove.txt', size: 10, extension: 'txt', metadata: {} }] });
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/health')) return response({ status: 'ok' });
      if (init?.method === 'DELETE') return response(session({ title: active.title }));
      return response(url.endsWith('/sessions') ? [active] : active);
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup(); render(<App />);
    await user.click(await screen.findByRole('button', { name: /Remove review/ }));
    await user.click(await screen.findByRole('button', { name: 'Remove remove.txt' }));
    await waitFor(() => expect(screen.queryByText('remove.txt')).not.toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledWith('/api/sessions/session-1/documents/doc-1', { method: 'DELETE' });
  });

  it('polls a processing session and displays the returned report findings', async () => {
    const active = session({ title: 'Pipeline review', status: 'ready', documents: [{ id: 'd', name: 'terms.txt', size: 10, extension: 'txt', metadata: {} }] });
    const complete = { ...active, status: 'completed', steps: [{ agent: 'validation_agent', status: 'completed', detail: 'Checks complete' }], report: { id: 'r', created_at: active.created_at, decision: 'needs_review', summary: 'An effective date needs attention.', findings: [{ rule_id: 'r-1', rule_name: 'Effective date', status: 'review', detail: 'Date not present.', document: 'terms.txt', evidence: 'Agreement between parties' }], document_count: 1 } };
    let started = false;
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url.endsWith('/health')) return response({ status: 'ok' });
      if (url.endsWith('/process')) { started = true; return response({ ...active, status: 'processing', steps: [{ agent: 'validation_agent', status: 'running', detail: 'Checking terms' }] }, 202); }
      if (url.endsWith('/sessions')) return response([active]);
      return response(started ? complete : active);
    }));
    const user = userEvent.setup(); render(<App />);
    await user.click(await screen.findByRole('button', { name: /Pipeline review/ }));
    await user.click(await screen.findByRole('button', { name: 'Process documents' }));
    expect(await screen.findByText('Checking terms')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Processing documents' })).toBeDisabled();
    expect(await screen.findByText('Your review is ready.', {}, { timeout: 4000 })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'View report' }));
    expect(screen.getByText('Date not present.')).toBeInTheDocument();
    expect(screen.getByText('Agreement between parties')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Download HTML' })).toBeEnabled();
  });
});