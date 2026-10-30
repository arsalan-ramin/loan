import { useCallback, useEffect, useState } from 'react';
import { AlertCircle, ArrowDownToLine, ChevronLeft, ChevronRight, ListChecks, LoaderCircle, Play, RefreshCw, Square } from 'lucide-react';
import { api, downloadBatchResults } from './api';
import type { BatchResultPage, BatchState } from './types';

const PAGE_SIZE = 25;
const FILTERS = ['', 'Approved', 'Decline', 'Refer', 'Error'] as const;

const runLabels: Record<BatchState['status'], string> = {
  none: 'Not started',
  running: 'Running',
  completed: 'Completed',
  cancelled: 'Cancelled',
  failed: 'Failed',
  interrupted: 'Interrupted',
};

// Test fetch stubs can answer unknown URLs with other shapes; only accept real batch payloads.
const isBatchState = (value: unknown): value is BatchState =>
  !!value && typeof value === 'object' && 'processed' in value && 'dataset_loan_count' in value;
const isResultPage = (value: unknown): value is BatchResultPage =>
  !!value && typeof value === 'object' && Array.isArray((value as BatchResultPage).results) && 'total' in value;

export default function BatchReview() {
  const [state, setState] = useState<BatchState | null>(null);
  const [page, setPage] = useState<BatchResultPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');

  const refresh = useCallback(async () => {
    const [next, results] = await Promise.all([api.batchStatus(), api.batchResults(offset, PAGE_SIZE, filter)]);
    if (isBatchState(next)) setState(next);
    if (isResultPage(results)) setPage(results);
  }, [offset, filter]);

  useEffect(() => {
    refresh().catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'Unable to load batch review.'));
  }, [refresh]);

  const running = state?.status === 'running';
  useEffect(() => {
    if (!running) return undefined;
    const timer = window.setInterval(() => { refresh().catch(() => undefined); }, 2000);
    return () => window.clearInterval(timer);
  }, [running, refresh]);

  const act = async (label: string, work: () => Promise<unknown>) => {
    setBusy(label);
    setError('');
    try {
      await work();
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : `${label} failed.`);
    } finally {
      setBusy('');
    }
  };

  const loanCount = state?.dataset_loan_count ?? 0;
  const datasetReady = state?.dataset_status === 'ready' && loanCount > 0;
  const total = state?.total || loanCount;
  const processed = state?.processed ?? 0;
  const percent = total ? Math.min(100, Math.round((processed / total) * 100)) : 0;
  const canResume = !!state && state.dataset_current && !running && ['cancelled', 'failed', 'interrupted', 'completed'].includes(state.status)
    && (processed < total || state.errors > 0);

  const start = () => {
    if (!window.confirm(`Review all ${loanCount} stored loans? This makes one paid Claude call per loan and replaces the previous batch results.`)) return;
    void act('Starting batch review', () => api.startBatch(false));
  };

  return <section className="card batch-card" aria-label="Batch review">
    <div className="card-heading">
      <div className="heading-with-icon"><ListChecks size={19} /><h2>Batch review</h2>{state && <span className={`batch-run-status ${state.status}`}>{runLabels[state.status]}</span>}</div>
      <button className="icon-button" aria-label="Refresh batch review" disabled={!!busy} onClick={() => void act('Refreshing', async () => undefined)}><RefreshCw size={17} /></button>
    </div>
    <div className="batch-body">
      <p className="batch-intro">
        {datasetReady
          ? `Stored data: ${loanCount} loans. Each loan gets an Approved, Decline or Refer status from the decision flow, with a Claude rationale. Only the latest run is kept.`
          : state?.dataset_status === 'indexing' ? 'The stored loan data is still indexing. Try again in a moment.'
            : 'No stored loan data yet. Upload the Query 1, 2 and 4 workbooks in Document studio first.'}
      </p>
      {error && <div className="error-banner" role="alert"><AlertCircle size={18} /><span>{error}</span></div>}
      {state?.error && state.status !== 'running' && <div className="error-banner" role="status"><AlertCircle size={18} /><span>{state.error}</span></div>}

      <div className="batch-actions">
        <button className="button primary" disabled={!datasetReady || running || !!busy} onClick={start}>
          {busy === 'Starting batch review' ? <LoaderCircle size={16} className="spin" /> : <Play size={16} />}Start batch review
        </button>
        {canResume && <button className="button secondary" disabled={!!busy} onClick={() => void act('Resuming batch review', () => api.startBatch(true))}><RefreshCw size={16} />Resume remaining</button>}
        {running && <button className="button secondary" disabled={!!busy} onClick={() => void act('Cancelling batch review', () => api.cancelBatch())}><Square size={16} />Cancel</button>}
        <button className="button secondary" disabled={!processed || !!busy} onClick={() => void act('Downloading Excel', downloadBatchResults)}><ArrowDownToLine size={16} />Download Excel</button>
      </div>

      {state && state.status !== 'none' && <div className="batch-progress">
        <div className="batch-progress-label"><span>{running && <LoaderCircle size={14} className="spin" />}{processed} of {total} loans processed</span><strong>{percent}%</strong></div>
        <div className="batch-bar" role="progressbar" aria-label="Batch progress" aria-valuemin={0} aria-valuemax={total} aria-valuenow={processed}><span style={{ width: `${percent}%` }} /></div>
        <div className="batch-counts">
          <span className="batch-status Approved">Approved {state.approved}</span>
          <span className="batch-status Decline">Decline {state.declined}</span>
          <span className="batch-status Refer">Refer {state.referred}</span>
          {state.errors > 0 && <span className="batch-status Error">Errors {state.errors}</span>}
        </div>
      </div>}

      {!!page && (page.total > 0 || filter) && <>
        <div className="batch-filter">
          <label htmlFor="batch-filter">Show</label>
          <select id="batch-filter" value={filter} onChange={(event) => { setOffset(0); setFilter(event.target.value as (typeof FILTERS)[number]); }}>
            {FILTERS.map((value) => <option key={value} value={value}>{value || 'All results'}</option>)}
          </select>
          <span>{page.total} {page.total === 1 ? 'loan' : 'loans'}</span>
        </div>
        <div className="batch-table-wrap">
          <table className="batch-table">
            <thead><tr><th>Loan ID</th><th>Account ID</th><th>Status</th><th>Rationale / result</th></tr></thead>
            <tbody>
              {page.results.map((row) => <tr key={row.loan_id}>
                <td>{row.loan_id}</td>
                <td>{row.account_id ?? '—'}</td>
                <td><span className={`batch-status ${row.status ?? 'Error'}`}>{row.status ?? 'Error'}</span></td>
                <td className="batch-rationale">{row.rationale ?? row.error ?? ''}{row.rationale && row.error && <em>{row.error}</em>}{!row.rationale && row.error && row.engine_rationale && <small>Decision flow: {row.engine_rationale}</small>}</td>
              </tr>)}
            </tbody>
          </table>
        </div>
        {page.total > PAGE_SIZE && <div className="batch-pager">
          <button className="icon-button" aria-label="Previous results" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}><ChevronLeft size={17} /></button>
          <span>{offset + 1}–{Math.min(offset + PAGE_SIZE, page.total)} of {page.total}</span>
          <button className="icon-button" aria-label="Next results" disabled={offset + PAGE_SIZE >= page.total} onClick={() => setOffset(offset + PAGE_SIZE)}><ChevronRight size={17} /></button>
        </div>}
      </>}
    </div>
    {busy && <div className="sr-only" role="status">{busy}</div>}
  </section>;
}
