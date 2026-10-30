import type { AccountCandidatePage, BatchResultPage, BatchState, LoanCandidatePage, Mode, Rule, Session, StoredDataset } from './types';

const BASE = '/api';
const segment = encodeURIComponent;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, init);
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new Error('Unable to reach Agent. Check that the backend is running and try again.');
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(typeof body?.detail === 'string' ? body.detail : `Request failed (${response.status}). Please try again.`);
  }
  return response.json() as Promise<T>;
}

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});

export const api = {
  health: () => request<{ status: string }>('/health'),
  sessions: () => request<Session[]>('/sessions'),
  create: () => request<Session>('/sessions', json('POST', {})),
  session: (id: string, signal?: AbortSignal) => request<Session>(`/sessions/${segment(id)}`, { signal }),
  message: (id: string, content: string, loanId?: string) => request<Session>(`/sessions/${segment(id)}/messages`, json('POST', { content, ...(loanId ? { loan_id: loanId } : {}) })),
  preflight: (id: string) => request<Session>(`/sessions/${segment(id)}/preflight`, { method: 'POST' }),
  accounts: (id: string, query = '', offset = 0, limit = 50) => request<AccountCandidatePage>(
    `/sessions/${segment(id)}/accounts?q=${encodeURIComponent(query)}&offset=${offset}&limit=${limit}`,
  ),
  loans: (id: string, query = '', offset = 0, limit = 50) => request<LoanCandidatePage>(
    `/sessions/${segment(id)}/loans?q=${encodeURIComponent(query)}&offset=${offset}&limit=${limit}`,
  ),
  selectAccount: (id: string, accountId: string) => request<Session>(
    `/sessions/${segment(id)}/account`, json('PUT', { account_id: accountId }),
  ),
  selectLoan: (id: string, loanId: string) => request<Session>(
    `/sessions/${segment(id)}/loan`, json('PUT', { loan_id: loanId }),
  ),
  inspectLoan: (id: string, loanId: string, topic: 'blockers' | 'rules' | 'evidence') => request<Session>(`/sessions/${segment(id)}/assistant/inspect`, json('POST', { loan_id: loanId, topic })),
  guidedProcess: (id: string, revision: string) => request<Session>(`/sessions/${segment(id)}/assistant/process`, json('POST', { confirmed: true, expected_updated_at: revision })),
  loanReview: (id: string, loanId: string, reportId: string, revision: string, preferredEvidenceRef?: string) => request<Session>(
    `/sessions/${segment(id)}/assistant/loan-review`, json('POST', {
      loan_id: loanId, report_id: reportId, expected_updated_at: revision, confirmed: true,
      ...(preferredEvidenceRef ? { preferred_evidence_ref: preferredEvidenceRef } : {}),
    }),
  ),
  reviewNote: (id: string, reportId: string, loanId: string, text: string, kind: 'note' | 'acknowledgment') => request<Session>(`/sessions/${segment(id)}/assistant/notes`, json('POST', { report_id: reportId, loan_id: loanId, text, kind })),
  upload: (id: string, files: File[]) => {
    const body = new FormData();
    files.forEach((file) => body.append('files', file));
    return request<Session>(`/sessions/${segment(id)}/documents`, { method: 'POST', body });
  },
  remove: (id: string, docId: string) => request<Session>(`/sessions/${segment(id)}/documents/${segment(docId)}`, { method: 'DELETE' }),
  storedDataset: () => request<StoredDataset>('/dataset'),
  batchStatus: () => request<BatchState>('/batch'),
  batchResults: (offset = 0, limit = 50, status = '') => request<BatchResultPage>(
    `/batch/results?offset=${offset}&limit=${limit}${status ? `&status=${encodeURIComponent(status)}` : ''}`,
  ),
  startBatch: (resume = false) => request<BatchState>('/batch', json('POST', { resume })),
  retryBatch: (loanIds?: string[]) => request<BatchState>('/batch', json('POST', loanIds ? { loan_ids: loanIds } : { failed_only: true })),
  cancelBatch: () => request<BatchState>('/batch/cancel', { method: 'POST' }),
  useStoredDataset: (id: string) => request<Session>(`/sessions/${segment(id)}/stored-dataset`, { method: 'POST' }),
  rules: (id: string, rules: Rule[]) => request<Session>(`/sessions/${segment(id)}/rules`, json('PUT', { rules })),
  mode: (id: string, mode: Mode) => request<Session>(`/sessions/${segment(id)}/mode`, json('PUT', { mode })),
  process: (id: string) => request<Session>(`/sessions/${segment(id)}/process`, { method: 'POST' }),
  reportUrl: (id: string) => `${BASE}/sessions/${segment(id)}/report`,
};

export async function downloadReport(id: string): Promise<void> {
  return downloadBlob(api.reportUrl(id), `agent-report-${id}.html`);
}

export async function downloadSourceDocument(sessionId: string, documentId: string, filename: string): Promise<void> {
  return downloadBlob(
    `${BASE}/sessions/${segment(sessionId)}/documents/${segment(documentId)}/download`,
    filename,
  );
}

export type UnderwritingFormat = 'json' | 'csv';

export async function downloadUnderwriting(id: string, format: UnderwritingFormat): Promise<void> {
  return downloadBlob(`${BASE}/sessions/${segment(id)}/underwriting?format=${format}`, `agent-underwriting-${id}.${format}`);
}

export async function downloadBatchResults(): Promise<void> {
  const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, '');
  return downloadBlob(`${BASE}/batch/export`, `loan-batch-results-${stamp}.xlsx`);
}

async function downloadBlob(path: string, filename: string): Promise<void> {
  let response: Response;
  try {
    response = await fetch(path);
  } catch {
    throw new Error('Unable to reach Agent. Check that the backend is running and retry the download.');
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(typeof body?.detail === 'string' ? body.detail : 'The report could not be downloaded. Please try again.');
  }
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.append(link);
  try {
    link.click();
  } finally {
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}