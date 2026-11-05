import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import TokenUsage from './TokenUsage';
import type { UsagePage } from './types';

const page: UsagePage = {
  totals: { requests: 2, reported_requests: 1, unreported_requests: 1, running_requests: 0,
    failed_requests: 1, input_tokens: 10, output_tokens: 5, cache_creation_input_tokens: 3,
    cache_read_input_tokens: 7, total_tokens: 25 },
  total: 2, offset: 0, limit: 25,
  requests: [
    { id: 'request1', started_at: '2026-11-04T12:00:00Z', http_request_id: 'http1', session_id: 's1',
      loan_id: 'L1', batch_id: null, operation: 'chat', model: 'model', status: 'succeeded',
      category: 'success', attempts: 1, duration_ms: 100, input_tokens: 10, output_tokens: 5,
      cache_creation_input_tokens: 3, cache_read_input_tokens: 7, total_tokens: 25 },
    { id: 'request2', started_at: '2026-11-04T12:01:00Z', http_request_id: null, session_id: null,
      loan_id: 'L2', batch_id: 'batch1', operation: 'batch_loan_review', model: 'model', status: 'failed',
      category: 'timeout', attempts: 3, duration_ms: 500, input_tokens: null, output_tokens: null,
      cache_creation_input_tokens: null, cache_read_input_tokens: null, total_tokens: null },
  ],
};

describe('Token usage', () => {
  it('shows totals and each request, distinguishing unknown counts from zero', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(page))));
    render(<TokenUsage sessionId="s1" />);
    const table = await screen.findByRole('table');
    expect(screen.getByText('Total reported tokens').nextElementSibling).toHaveTextContent('25');
    expect(within(table).getByText('request1')).toBeInTheDocument();
    expect(within(table).getByRole('cell', { name: /batch_loan_review/ })).toBeInTheDocument();
    expect(within(table).getAllByText('Unknown')).toHaveLength(5);
    expect(screen.getByText(/1 with unreported usage/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next requests' })).toBeDisabled();
  });

  it('filters session totals, paginates and refreshes without a provider call', async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify({ ...page, total: 30 })));
    vi.stubGlobal('fetch', fetch);
    const user = userEvent.setup();
    render(<TokenUsage sessionId="session with space" />);
    await screen.findByRole('table');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Usage scope' }), 'session');
    await waitFor(() => expect(fetch).toHaveBeenLastCalledWith('/api/usage?offset=0&limit=25&session_id=session%20with%20space', expect.anything()));
    await screen.findByRole('table');
    await user.click(screen.getByRole('button', { name: 'Next requests' }));
    await waitFor(() => expect(fetch).toHaveBeenLastCalledWith('/api/usage?offset=25&limit=25&session_id=session%20with%20space', expect.anything()));
    await screen.findByRole('table');
    await user.click(screen.getByRole('button', { name: 'Refresh usage' }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(4));
  });

  it('shows load errors explicitly and allows a refresh', async () => {
    const fetch = vi.fn().mockRejectedValue(new Error('offline'));
    vi.stubGlobal('fetch', fetch);
    const user = userEvent.setup();
    render(<TokenUsage />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Unable to reach Agent');
    fetch.mockResolvedValue(new Response(JSON.stringify({ ...page, requests: [], total: 0 })));
    await user.click(screen.getByRole('button', { name: 'Refresh usage' }));
    expect(await screen.findByText(/No Claude requests recorded/)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
