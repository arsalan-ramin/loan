import { useEffect, useState } from 'react';
import { api } from './api';
import type { UsagePage } from './types';

const count = (value: number | null) => value === null ? 'Unknown' : value.toLocaleString();

export default function TokenUsage({ sessionId }: { sessionId?: string }) {
  const [sessionOnly, setSessionOnly] = useState(false);
  const [offset, setOffset] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [data, setData] = useState<UsagePage | null>(null);
  const [error, setError] = useState('');
  const scope = sessionOnly ? sessionId : undefined;

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    setData(null);
    async function load() {
      try {
        const next = await api.usage(scope, offset, controller.signal);
        if (controller.signal.aborted) return;
        setData(next);
        setError('');
      } catch (reason) {
        if (controller.signal.aborted) return;
        setError(reason instanceof Error ? reason.message : 'Unable to load token usage.');
      }
      if (!controller.signal.aborted) timer = setTimeout(load, 5000);
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [scope, offset, refresh]);

  return <section className="card token-usage" aria-label="Token usage">
    <div className="card-heading"><h2>Token usage</h2><button className="button secondary" onClick={() => setRefresh((value) => value + 1)}>Refresh usage</button></div>
    <div className="usage-body">
      <label>Usage scope <select aria-label="Usage scope" value={sessionOnly ? 'session' : 'all'} onChange={(event) => {
        setSessionOnly(event.target.value === 'session'); setOffset(0);
      }}><option value="all">Entire workspace</option><option value="session" disabled={!sessionId}>Current session</option></select></label>
      <p>Provider-reported tokens since tracking was enabled. Local lookups and greetings make no Claude request. Totals survive restarts, new batches, and dataset replacement. Automatically refreshes every 5 seconds.</p>
      <p>One row per Claude invocation, including retries in its attempt count. Input excludes cache tokens; total includes input, output, cache writes and cache reads. Unknown usage is not zero. Failed network attempts may incur usage the provider did not report. These are token counts, not billing estimates. “Succeeded” means the provider returned complete text, not that commentary passed application validation.</p>
      {error && <p role="alert">{error} Displayed counts may be stale.</p>}
      {!data && !error && <p role="status">Loading token usage…</p>}
      {data && <>
        <dl className="usage-totals">
          <div><dt>Total reported tokens</dt><dd>{count(data.totals.total_tokens)}</dd></div>
          <div><dt>Input</dt><dd>{count(data.totals.input_tokens)}</dd></div>
          <div><dt>Output</dt><dd>{count(data.totals.output_tokens)}</dd></div>
          <div><dt>Cache writes</dt><dd>{count(data.totals.cache_creation_input_tokens)}</dd></div>
          <div><dt>Cache reads</dt><dd>{count(data.totals.cache_read_input_tokens)}</dd></div>
        </dl>
        <p>{data.total.toLocaleString()} requests · {data.totals.reported_requests.toLocaleString()} with reported usage · {data.totals.unreported_requests.toLocaleString()} with unreported usage · {data.totals.running_requests.toLocaleString()} running · {data.totals.failed_requests.toLocaleString()} failed/interrupted</p>
        <div className="usage-table"><table>
          <caption>Claude requests, newest first</caption>
          <thead><tr>{['Request / time', 'Operation / scope', 'Model', 'Status / attempts', 'Input', 'Output', 'Cache write', 'Cache read', 'Total'].map((label) => <th scope="col" key={label}>{label}</th>)}</tr></thead>
          <tbody>{data.requests.map((request) => <tr key={request.id}>
            <th scope="row"><code>{request.id}</code><br />{new Date(request.started_at).toLocaleString()}{request.http_request_id && <><br />HTTP: <code>{request.http_request_id}</code></>}</th>
            <td>{request.operation}<br />{request.loan_id && `Loan ${request.loan_id}`}<br />{request.session_id && `Session ${request.session_id}`}{request.batch_id && `Batch ${request.batch_id}`}</td>
            <td>{request.model}</td><td>{request.status} / {request.attempts}<br />{request.category}{request.duration_ms !== null && <><br />{Math.round(request.duration_ms).toLocaleString()} ms</>}</td>
            <td>{count(request.input_tokens)}</td><td>{count(request.output_tokens)}</td><td>{count(request.cache_creation_input_tokens)}</td><td>{count(request.cache_read_input_tokens)}</td><td>{count(request.total_tokens)}</td>
          </tr>)}</tbody>
        </table></div>
        {data.total === 0 && <p>No Claude requests recorded for this scope yet. Earlier requests cannot be reconstructed.</p>}
        <div className="usage-pagination"><button className="button secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 25))}>Previous requests</button><span>{data.total ? `${offset + 1}–${Math.min(offset + 25, data.total)} of ${data.total}` : '0 requests'}</span><button className="button secondary" disabled={offset + 25 >= data.total} onClick={() => setOffset(offset + 25)}>Next requests</button></div>
      </>}
    </div>
  </section>;
}
