import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import App from './App';
import type { PolicyRuleDefinition, Session } from './types';

// Mock server catalog: definitions and page citations are data, never UI constants.
const catalog: PolicyRuleDefinition[] = [
  { id: 'MTB-POLICY-SIGNOFF', name: 'Policy-owner signoff', category: 'Governance', page: 4, description: 'Design proposal is not approved credit policy.', state: 'enforced', reason: 'Authorized policy and lender signoff required.' },
  { id: 'MTB-IDENTITY', name: 'Explicit identity mapping', category: 'Evidence safeguards', page: 1, description: 'Rows are never joined by position.', state: 'enforced', reason: 'Account, loan ID and borrower role mappings must be validated.' },
  { id: 'MTB-BIL-LETTER', name: 'BIL acceptance letter', category: 'Product documentation', page: 2, description: 'BIL acceptance letter is required.', state: 'conditional', reason: 'Only applies to BIL with valid evidence.' },
  { id: 'MTB-CURRENCY-UNITS', name: 'Currency units', category: 'Amounts', page: 1, description: 'Confirm source currency units before applying boundaries.', state: 'blocked', reason: 'Source currency units are unconfirmed.' },
  { id: 'MTB-SCORE-SBSS', name: 'SBSS scoring minimum', category: 'Scoring', page: 3, description: 'Approved scoring minimum and valid source value are required.', state: 'blocked', reason: 'No approved scoring threshold supplied.' },
];
const activeSession = (overrides: Partial<Session> = {}): Session => ({
  id: 'policy-session', title: 'Policy catalog session', created_at: '2026-10-20T12:00:00Z', updated_at: '2026-10-20T12:00:00Z',
  status: 'ready', documents: [{ id: 'd', name: 'MTB_Query_1.xlsx', extension: '.xlsx', size: 100, metadata: {} }],
  messages: [], rules: [], steps: [], report: null, error: null, policy_rules: catalog, ...overrides,
});
const response = (body: unknown) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
async function openSession(active: Session) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith('/health')) return response({ status: 'ok' });
    if (url.endsWith('/rules')) return response({ ...active, rules: JSON.parse(init!.body as string).rules });
    return response(url.endsWith('/sessions') ? [active] : active);
  });
  vi.stubGlobal('fetch', fetchMock);
  const user = userEvent.setup();
  const rendered = render(<App />);
  await user.click(await screen.findByRole('button', { name: /Policy catalog session/ }));
  await waitFor(() => expect(screen.getByLabelText('Choose documents')).toBeEnabled());
  return { user, fetchMock, ...rendered };
}

it.each([1, 2, 4])('shows server-classified counts for MTB_Query_%s uploads, not generic active rules', async (number) => {
  await openSession(activeSession({ documents: [{ id: 'd', name: `MTB_Query_${number}.xlsx`, extension: '.xlsx', size: 100, metadata: {} }] }));
  const card = screen.getByRole('region', { name: 'Underwriting rules' });
  expect(within(card).getByText('2 enforced safeguards')).toBeInTheDocument();
  expect(within(card).getByText('1 conditional checks')).toBeInTheDocument();
  expect(within(card).getByText('2 blocked checks')).toBeInTheDocument();
  expect(card).not.toHaveTextContent(/active|minimum word/i);
  expect(card).toHaveTextContent('Draft guideline, not approved credit policy. No lending decisions');
  expect(card).toHaveTextContent('not a claim of evaluation');
});

it.each(['MTB_Query_1_Sample_Data.xlsx', 'mtb-query-2-sAmPlE.XLSX', 'MtB Query 4 (Sample Data).xLsX'])('recognizes suffixed/case-varied %s without a server catalog', async (name) => {
  await openSession(activeSession({ policy_rules: [], documents: [{ id: 'd', name, extension: '.xlsx', size: 100, metadata: {} }] }));
  expect(screen.getByRole('region', { name: 'Underwriting rules' })).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Review rules' })).not.toBeInTheDocument();
  expect(screen.getByText('No guideline rules were supplied by the server.')).toBeInTheDocument();
});

it('recognizes an arbitrarily renamed workbook from its server catalog', async () => {
  await openSession(activeSession({ documents: [{ id: 'd', name: 'renamed.xlsx', extension: '.xlsx', size: 100, metadata: {} }] }));
  expect(screen.getByRole('region', { name: 'Underwriting rules' })).toBeInTheDocument();
  expect(screen.getByText('2 enforced safeguards')).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Review rules' })).not.toBeInTheDocument();
});

it('renders every server definition and page as a safe, read-only catalog', async () => {
  const unsafe = '<img src=x onerror=alert(1)>';
  const { user, container, fetchMock } = await openSession(activeSession({ policy_rules: [...catalog, {
    id: 'server-added', name: unsafe, category: '<b>Server category</b>', page: 17,
    description: '<script>alert(1)</script>', state: 'conditional', reason: '<svg onload=alert(1)>',
  }] }));
  expect(screen.getByText('2 conditional checks')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'View guideline rules' }));
  const dialog = screen.getByRole('dialog', { name: 'Underwriting rules' });
  for (const rule of catalog) {
    const item = within(dialog).getByRole('heading', { name: rule.name }).closest('article')!;
    expect(within(item).getByText(`${rule.category} · PDF page ${rule.page}`)).toBeInTheDocument();
    expect(within(item).getByText(`State: ${rule.state}`)).toBeInTheDocument();
    expect(within(item).getByText(rule.description)).toBeInTheDocument();
    expect(item).toHaveTextContent(rule.reason);
  }
  expect(within(dialog).getByRole('heading', { name: unsafe })).toBeInTheDocument();
  expect(within(dialog).getByText('<b>Server category</b> · PDF page 17')).toBeInTheDocument();
  expect(within(dialog).getByText('<script>alert(1)</script>')).toBeInTheDocument();
  expect(dialog).toHaveTextContent('<svg onload=alert(1)>');
  expect(dialog).toHaveTextContent('No lending approval or denial decisions.');
  expect(dialog).toHaveTextContent('inclusion here does not mean a check was evaluated');
  expect(dialog.querySelector('input, select, textarea, [contenteditable]')).toBeNull();
  expect(container.querySelector('img, script, .policy-catalog-category b, svg[onload]')).toBeNull();
  await user.click(within(dialog).getByRole('button', { name: 'Close guideline rules' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(false);
});

it('keeps custom document checks and their editor separate from underwriting', async () => {
  const { user, fetchMock } = await openSession(activeSession({ rules: [
    { id: 'custom', name: 'Minimum words', kind: 'min_words', value: '250', severity: 'review', enabled: true },
    { id: 'draft', name: 'Draft text', kind: 'forbidden_text', value: 'draft', severity: 'review', enabled: false },
  ] }));
  const underwriting = screen.getByRole('region', { name: 'Underwriting rules' });
  expect(underwriting).not.toHaveTextContent(/minimum word|draft text|active/i);
  expect(within(underwriting).queryByRole('button', { name: 'Edit document checks' })).not.toBeInTheDocument();
  const optional = screen.getByRole('region', { name: 'Optional document checks' });
  expect(optional).toHaveTextContent('1 enabled document checks · 2 configured');
  await user.click(within(optional).getByRole('button', { name: 'Edit document checks' }));
  const dialog = screen.getByRole('dialog', { name: 'Optional document checks' });
  expect(dialog).toHaveTextContent('separate from underwriting rules');
  expect(within(dialog).getAllByRole('textbox', { name: 'Rule name' })[0]).toHaveValue('Minimum words');
  await user.click(within(dialog).getByRole('button', { name: 'Save rules' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  const update = fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT');
  expect(JSON.parse(update![1]!.body as string)).not.toHaveProperty('policy_rules');
  expect(screen.getByText('2 enforced safeguards')).toBeInTheDocument();
});

it.each([undefined, []])('handles an absent or empty server catalog without inventing requirements (%s)', async (rules) => {
  const { user } = await openSession(activeSession({ policy_rules: rules }));
  expect(screen.getByText('0 enforced safeguards')).toBeInTheDocument();
  expect(screen.getByText('0 conditional checks')).toBeInTheDocument();
  expect(screen.getByText('0 blocked checks')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'View guideline rules' }));
  const dialog = screen.getByRole('dialog');
  expect(dialog).toHaveTextContent('No guideline rules were supplied by the server. No policy checks are claimed evaluated.');
  expect(dialog.querySelectorAll('article')).toHaveLength(0);
  await user.click(within(dialog).getByRole('button', { name: 'Close' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('uses report policy evaluation to identify MTB even without the original uploaded filenames', async () => {
  await openSession(activeSession({ documents: [], policy_rules: [], report: {
    id: 'r', created_at: '2026-10-20T12:00:00Z', decision: 'needs_review', summary: 'Draft route only.', document_count: 0, findings: [],
    policy_evaluation: {
      policy_version: 'draft-v1', source_document: 'guide.pdf', source_sha256: null, policy_status: 'draft', recommended_route: 'MANUAL_REVIEW',
      decision_recommendation: null, requires_human_approval: true, missing_information: [], rule_results: [], profiles: [], limitations: [],
    },
  } }));
  expect(screen.getByRole('region', { name: 'Underwriting rules' })).toBeInTheDocument();
  expect(screen.getByText('0 enforced safeguards')).toBeInTheDocument();
});

it.each([{ documents: [] }, { documents: [{ id: 'other', name: 'quarterly-report.xlsx', extension: '.xlsx', size: 100, metadata: {} }] }])('preserves generic rules behavior without MTB documents or a policy report', async ({ documents }) => {
  const { user } = await openSession(activeSession({ documents, policy_rules: [], rules: [
    { id: 'custom', name: 'Required date', kind: 'required_text', value: 'date', severity: 'review', enabled: true },
  ] }));
  expect(screen.getByRole('heading', { name: 'Review rules' })).toBeInTheDocument();
  expect(screen.getByText('1 active rule')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'View guideline rules' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Manage rules' }));
  expect(screen.getByRole('dialog', { name: 'Your standards. Set in rules.' })).toBeInTheDocument();
});

const legacyReport: NonNullable<Session['report']> = {
  id: 'legacy-report', created_at: '2026-10-20T12:00:00Z', decision: 'approved', summary: 'Legacy approval summary.', document_count: 1,
  findings: [{ rule_id: 'custom', rule_name: 'Required date', status: 'pass', detail: 'Date found.', document: 'renamed.xlsx', evidence: 'Original date evidence' }],
  llm_analysis: 'Legacy Claude approval commentary.', llm_model: 'legacy-model', llm_error: 'Legacy provider warning.',
};

it.each([
  { name: 'MTB_Query_1_Sample_Data.xlsx', rules: [] },
  { name: 'mtb-query-2-sAmPlE.XLSX', rules: [] },
  { name: 'MtB Query 4 (Sample Data).xLsX', rules: [] },
  { name: 'renamed.xlsx', rules: catalog },
].flatMap((item) => [true, false].map((explicitNull) => ({ ...item, explicitNull }))))('guards stale approved MTB report for $name (explicit null: $explicitNull)', async ({ name, rules, explicitNull }) => {
  const { user, container, fetchMock } = await openSession(activeSession({
    status: 'completed', policy_rules: rules,
    documents: [{ id: 'd', name, extension: '.xlsx', size: 100, metadata: {} }],
    report: { ...legacyReport, ...(explicitNull ? { policy_evaluation: null } : {}) },
  }));
  await user.click(screen.getByRole('button', { name: 'View report' }));
  expect(screen.getByRole('alert')).toHaveTextContent('Underwriting evaluation missing');
  expect(screen.getByRole('alert')).toHaveTextContent('This report contains only document checks. Run Process documents again to evaluate the guideline. It is not a lending approval.');
  expect(screen.getByRole('heading', { name: 'Document checks' })).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: /approved/i })).not.toBeInTheDocument();
  expect(container.querySelector('.decision-banner.approved')).toBeNull();
  expect(screen.queryByText(legacyReport.summary)).not.toBeInTheDocument();
  expect(screen.queryByRole('region', { name: 'Claude explanation' })).not.toBeInTheDocument();
  for (const text of [legacyReport.llm_analysis!, legacyReport.llm_error!, `Model: ${legacyReport.llm_model}`]) {
    expect(screen.queryByText(text)).not.toBeInTheDocument();
  }
  expect(screen.getByText('Date found.')).toBeInTheDocument();
  expect(screen.getByText('Original date evidence')).toBeInTheDocument();
  const download = screen.getByRole('button', { name: 'Download HTML' });
  expect(download).toBeDisabled();
  await user.click(download);
  expect(fetchMock.mock.calls.some(([url]) => url.includes('/report'))).toBe(false);
  await user.click(screen.getByRole('button', { name: 'Document studio' }));
  expect(screen.getByRole('button', { name: 'Process documents' })).toBeDisabled();
});

it.each(['renamed.xlsx', 'quarterly-report.xlsx'])('preserves generic approved reports and commentary for %s without a catalog', async (name) => {
  const { user, container } = await openSession(activeSession({
    status: 'completed', policy_rules: [],
    documents: [{ id: 'd', name, extension: '.xlsx', size: 100, metadata: {} }],
    report: { ...legacyReport, policy_evaluation: null },
  }));
  expect(screen.getByRole('heading', { name: 'Review rules' })).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'View report' }));
  expect(screen.getByRole('heading', { name: 'approved' })).toBeInTheDocument();
  expect(container.querySelector('.decision-banner.approved')).toBeInTheDocument();
  expect(screen.getByText(legacyReport.summary)).toBeInTheDocument();
  expect(screen.getByRole('region', { name: 'Claude explanation' })).toHaveTextContent(legacyReport.llm_analysis!);
  expect(screen.queryByText('Underwriting evaluation missing')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Download HTML' })).toBeEnabled();
});