import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import BatchReview from './BatchReview';
import type { BatchResultPage, BatchState } from './types';

const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

const state = (overrides: Partial<BatchState> = {}): BatchState => ({
  status: 'none', job_mode: null, job_total: 0, job_done: 0, retrying_loan_ids: [],
  started_at: null, finished_at: null, total: 0, processed: 0, approved: 0, declined: 0, referred: 0,
  errors: 0, model: null, error: null, dataset_status: 'ready', dataset_loan_count: 3, dataset_current: false, concurrency: 4,
  ...overrides,
});

const page = (overrides: Partial<BatchResultPage> = {}): BatchResultPage => ({ results: [], total: 0, offset: 0, limit: 25, ...overrides });

const completedPage = page({
  total: 3,
  results: [
    { loan_id: 'L1', account_id: 'A1', status: 'Approved', branch: 'APPROVE', rationale: 'Result: L1 is Approve.\nWhy: all criteria pass.', engine_rationale: 'ok', error: null, processed_at: 'x', processing: 'succeeded' },
    { loan_id: 'L2', account_id: 'A2', status: 'Decline', branch: 'KNOWN_KNOCKOUT', rationale: 'Result: L2 is Decline.', engine_rationale: 'fico', error: null, processed_at: 'x', processing: 'succeeded' },
    { loan_id: 'L3', account_id: null, status: 'Refer', branch: 'REFER', rationale: null, engine_rationale: 'missing', error: 'Claude rationale unavailable: timeout', processed_at: 'x', processing: 'failed' },
    { loan_id: 'L4', account_id: 'A4', status: 'Refer', branch: 'REFER', rationale: null, engine_rationale: 'missing', error: 'Claude rationale unavailable: timeout', processed_at: 'x', processing: 'failed' },
  ],
});

afterEach(() => vi.restoreAllMocks());

describe('BatchReview', () => {
  it('confirms the paid run, starts it, shows progress and lists results', async () => {
    let current = state();
    let results = page();
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === '/api/batch' && init?.method === 'POST') {
        expect(JSON.parse(String(init.body))).toEqual({ resume: false });
        current = state({ status: 'completed', total: 4, processed: 4, approved: 1, declined: 1, referred: 2, errors: 2, dataset_current: true });
        results = completedPage;
        return response(current, 202);
      }
      if (url === '/api/batch') return response(current);
      if (url.startsWith('/api/batch/results')) return response(results);
      return response({ detail: 'unexpected' }, 404);
    });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<BatchReview />);
    await screen.findByText(/Stored data: 3 loans/);
    expect(screen.getByRole('button', { name: 'Download Excel' })).toBeDisabled();

    await userEvent.setup().click(screen.getByRole('button', { name: 'Start batch review' }));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('one paid Claude call per loan'));
    await screen.findByText('4 of 4 loans processed');
    expect(screen.getByRole('progressbar', { name: 'Batch progress' })).toHaveAttribute('aria-valuenow', '4');
    const table = screen.getByRole('table');
    expect(within(table).getAllByRole('columnheader').map((cell) => cell.textContent)).toEqual(['', 'Loan ID', 'Account ID', 'Status', 'Processing', 'Rationale / result', 'Actions']);
    expect(within(table).getByText(/Why: all criteria pass/)).toBeInTheDocument();
    expect(within(table).getAllByText(/Claude rationale unavailable/)).toHaveLength(2);
    expect(within(table).getAllByText('Succeeded')).toHaveLength(2);
    expect(within(table).getAllByText('Failed')).toHaveLength(2);
    expect(screen.getByRole('button', { name: 'Download Excel' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Resume remaining' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Retry all failed (2)' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Retry L1' })).toBeNull();
    expect(screen.queryByRole('checkbox', { name: 'Select L1' })).toBeNull();
    expect(fetchMock).toHaveBeenCalledWith('/api/batch', expect.objectContaining({ method: 'POST' }));
  });

  it('retries one failed record, a selection, or all failed records', async () => {
    const posts: unknown[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === '/api/batch' && init?.method === 'POST') {
        posts.push(JSON.parse(String(init.body)));
        return response(state({ status: 'running', job_mode: 'retry', job_total: 1, retrying_loan_ids: ['L3'], dataset_current: true }), 202);
      }
      if (url === '/api/batch') return response(state({ status: 'completed', total: 4, processed: 4, errors: 2, dataset_current: true }));
      return response(completedPage);
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const user = userEvent.setup();
    render(<BatchReview />);
    await screen.findByRole('button', { name: 'Retry L3' });

    await user.click(screen.getByRole('button', { name: 'Retry L3' }));
    await waitFor(() => expect(posts).toEqual([{ loan_ids: ['L3'] }]));

    await user.click(screen.getByRole('checkbox', { name: 'Select L3' }));
    await user.click(screen.getByRole('checkbox', { name: 'Select L4' }));
    await user.click(screen.getByRole('button', { name: 'Retry selected (2)' }));
    await waitFor(() => expect(posts[1]).toEqual({ loan_ids: ['L3', 'L4'] }));
    expect(screen.queryByRole('button', { name: /Retry selected/ })).toBeNull();

    await user.click(screen.getByRole('checkbox', { name: 'Select all failed loans on this page' }));
    expect(screen.getByRole('button', { name: 'Retry selected (2)' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Retry all failed (2)' }));
    await waitFor(() => expect(posts[2]).toEqual({ failed_only: true }));
  });

  it('hides record selection when no record failed', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => String(input) === '/api/batch'
      ? response(state({ status: 'completed', total: 2, processed: 2, approved: 1, declined: 1, dataset_current: true }))
      : response(page({ total: 2, results: completedPage.results.slice(0, 2) })));
    render(<BatchReview />);
    await screen.findByText('No failed records — nothing to retry.');
    expect(screen.queryByRole('checkbox')).toBeNull();
    expect(screen.queryByRole('button', { name: /Retry/ })).toBeNull();
  });

  it('shows retry progress and marks loans being retried', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => String(input) === '/api/batch'
      ? response(state({ status: 'running', job_mode: 'retry', job_total: 2, job_done: 1, retrying_loan_ids: ['L4'], total: 4, processed: 4, errors: 1, dataset_current: true }))
      : response(completedPage));
    render(<BatchReview />);
    await screen.findByText('Retrying 1 of 2 failed loans');
    expect(screen.getByText('Retrying')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry L4' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Retry L3' })).toBeDisabled();
  });

  it('does not start without confirmation and explains when no stored data exists', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => String(input) === '/api/batch'
      ? response(state({ dataset_status: 'empty', dataset_loan_count: 0 }))
      : response(page()));
    render(<BatchReview />);
    await screen.findByText(/No stored loan data yet/);
    expect(screen.getByRole('button', { name: 'Start batch review' })).toBeDisabled();
  });

  it('filters results by status and shows a running run with cancel', async () => {
    const requests: string[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      requests.push(url);
      if (url === '/api/batch') return response(state({ status: 'running', total: 3, processed: 1, approved: 1, dataset_current: true }));
      return response(page({ total: 1, results: [completedPage.results[0]] }));
    });
    render(<BatchReview />);
    await screen.findByText('1 of 3 loans processed');
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start batch review' })).toBeDisabled();
    await userEvent.setup().selectOptions(screen.getByLabelText('Show'), 'Decline');
    await waitFor(() => expect(requests).toContain('/api/batch/results?offset=0&limit=25&status=Decline'));
  });
});
