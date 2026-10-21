import { describe, expect, it, vi } from 'vitest';
import { api, downloadReport, downloadUnderwriting } from './api';

describe('API contract', () => {
  it('updates mode with only the exact mode contract and returns the invalidated session', async () => {
    const active = { id: 'session/a', mode: 'TEST', report: null, status: 'ready' };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(active)));
    vi.stubGlobal('fetch', fetchMock);
    await expect(api.mode('session/a', 'TEST')).resolves.toEqual(active);
    expect(fetchMock).toHaveBeenCalledWith('/api/sessions/session%2Fa/mode', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: 'TEST' }),
    });
  });

  it.each(['json', 'csv'] as const)('downloads %s as an untouched blob using the encoded endpoint', async (format) => {
    vi.useFakeTimers();
    const body = format === 'json' ? '{"amount":"90071992547409931234567890.1234567890"}' : 'amount\r\n90071992547409931234567890.1234567890\r\n';
    const blob = new Blob([body]);
    const json = vi.fn();
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, blob: vi.fn().mockResolvedValue(blob), json });
    vi.stubGlobal('fetch', fetchMock);
    const create = vi.fn().mockReturnValue('blob:underwriting');
    const revoke = vi.fn();
    vi.stubGlobal('URL', { createObjectURL: create, revokeObjectURL: revoke });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      expect(this.download).toBe(`agent-underwriting-session/a.${format}`);
      expect(this.href).toBe('blob:underwriting');
    });
    try {
      await downloadUnderwriting('session/a', format);
      expect(fetchMock).toHaveBeenCalledWith(`/api/sessions/session%2Fa/underwriting?format=${format}`);
      expect(create).toHaveBeenCalledWith(blob);
      expect(json).not.toHaveBeenCalled();
      expect(click).toHaveBeenCalledOnce();
      vi.runAllTimers();
      expect(revoke).toHaveBeenCalledWith('blob:underwriting');
      expect(document.querySelector('a[download]')).toBeNull();
    } finally {
      vi.useRealTimers();
      click.mockRestore();
    }
  });

  it.each(['json', 'csv'] as const)('surfaces %s export conflicts', async (format) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'Complete an underwriting review before downloading results.' }), { status: 409 })));
    await expect(downloadUnderwriting('s', format)).rejects.toThrow('Complete an underwriting review before downloading results.');
  });

  it('handles non-JSON export errors and network outages', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('Bad gateway', { status: 502 })));
    await expect(downloadUnderwriting('s', 'csv')).rejects.toThrow('could not be downloaded');
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    await expect(downloadUnderwriting('s', 'json')).rejects.toThrow('Unable to reach Agent');
  });

  it('loads the fixed dataset with an encoded session identifier and no request body', async () => {
    const active = { id: 'session/a', documents: [] };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(active)));
    vi.stubGlobal('fetch', fetchMock);
    await expect(api.loadDataset('session/a')).resolves.toEqual(active);
    expect(fetchMock).toHaveBeenCalledWith('/api/sessions/session%2Fa/dataset', { method: 'POST' });
  });

  it.each([404, 409])('surfaces dataset HTTP %s details', async (status) => {
    const detail = status === 404 ? 'Dataset files are missing.' : 'Only empty sessions can load the dataset.';
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail }), { status })));
    await expect(api.loadDataset('s')).rejects.toThrow(detail);
  });

  it('encodes session and document identifiers', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{}'));
    vi.stubGlobal('fetch', fetchMock);
    await api.remove('session/a', 'doc b');
    expect(fetchMock).toHaveBeenCalledWith('/api/sessions/session%2Fa/documents/doc%20b', { method: 'DELETE' });
  });

  it('downloads the backend HTML report without rendering untrusted HTML', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html><body>Report</body></html>', { headers: { 'Content-Type': 'text/html' } })));
    const create = vi.fn().mockReturnValue('blob:report');
    vi.stubGlobal('URL', { createObjectURL: create, revokeObjectURL: vi.fn() });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      expect(this.download).toBe('agent-report-session-1.html');
    });
    await downloadReport('session-1');
    expect(fetch).toHaveBeenCalledWith('/api/sessions/session-1/report');
    expect(create).toHaveBeenCalled();
    expect(click).toHaveBeenCalledOnce();
    vi.runAllTimers();
    vi.useRealTimers();
    click.mockRestore();
  });

  it('surfaces report download errors from the backend', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'Report is not ready.' }), { status: 409 })));
    await expect(downloadReport('s')).rejects.toThrow('Report is not ready.');
  });
});