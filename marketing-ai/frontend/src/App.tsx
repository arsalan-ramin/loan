import { ArrowDownToLine, ArrowRight, ArrowUp, BookOpen, CheckCircle2, FileText, FolderOpen, MessageSquare, RotateCcw, Sparkles, Trash2 } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { request } from './api';
import type { DocumentRecord, Message, UploadResult, Workspace } from './api';

const ACCEPT = '.pdf,.docx,.pptx,.xlsx,.csv,.txt,.md,.json';
const PROMPTS = ['Who is the target audience?', 'What are the campaign goals?', 'What metrics define success?'];
const LABELS: Record<string, string> = {
  format: 'File type', size_bytes: 'Size', uploaded_at: 'Uploaded', passages: 'Indexed passages',
  characters: 'Extracted characters', sha256: 'File fingerprint (SHA-256)', keys: 'JSON keys',
};

function bytes(value: number) {
  return value >= 1048576 ? `${(value / 1048576).toFixed(1)} MB`
    : value >= 1024 ? `${(value / 1024).toFixed(1)} KB` : `${value} B`;
}

function metadataValue(key: string, value: string | number | string[]) {
  if (key === 'size_bytes' && typeof value === 'number') return bytes(value);
  if (key === 'uploaded_at' && typeof value === 'string') return new Date(value).toLocaleString();
  return Array.isArray(value) ? value.join(', ') : value;
}

function SourceCard({ document, onRemove, disabled }: { document: DocumentRecord; onRemove: () => void; disabled: boolean }) {
  return <article className="source-card">
    <div className="source-heading">
      <span className="file-icon"><FileText size={18} /></span>
      <div><h3>{document.name}</h3><p>{document.metadata.format.toUpperCase()} &middot; {bytes(document.metadata.size_bytes)} &middot; {document.metadata.passages} indexed passages</p></div>
      <button className="icon-button" disabled={disabled} onClick={onRemove} aria-label={`Remove ${document.name}`}><Trash2 size={17} /></button>
    </div>
    <details className="metadata">
      <summary>View saved metadata</summary>
      <dl>{Object.entries(document.metadata).map(([key, value]) => <div key={key}>
        <dt>{LABELS[key] ?? key.replaceAll('_', ' ')}</dt><dd>{metadataValue(key, value)}</dd>
      </div>)}</dl>
    </details>
  </article>;
}

export function App() {
  const [workspace, setWorkspace] = useState<Workspace>();
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [tab, setTab] = useState<'library' | 'chat'>('library');
  const [scope, setScope] = useState('');
  const [draft, setDraft] = useState('');
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const chatEnd = useRef<HTMLDivElement>(null);
  const active = useRef(false);
  const docs = workspace?.documents ?? [];
  const messages = workspace?.messages ?? [];
  const disabled = loading || !!busy || !workspace;

  async function load() {
    setLoading(true);
    setError('');
    try { setWorkspace(await request<Workspace>('/workspace')); }
    catch (err) { setError(err instanceof Error ? err.message : 'Could not load the workspace.'); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);
  useEffect(() => {
    if (tab === 'chat' && workspace?.messages.length) chatEnd.current?.scrollIntoView({ block: 'nearest' });
  }, [tab, workspace?.messages.length]);

  async function perform(label: string, action: () => Promise<void>) {
    if (disabled || active.current) return;
    active.current = true;
    setBusy(label);
    setError('');
    setNotice('');
    try { await action(); }
    catch (err) { setError(err instanceof Error ? err.message : 'Something went wrong. Try again.'); }
    finally { active.current = false; setBusy(''); }
  }

  function upload(files: FileList | File[]) {
    if (!files.length) return;
    void perform('Extracting and indexing files…', async () => {
      if (files.length > 5) throw new Error('Choose at most five files per upload.');
      const body = new FormData();
      Array.from(files).forEach(file => body.append('files', file));
      const result = await request<UploadResult>('/documents', { method: 'POST', body });
      setWorkspace(current => current && { ...current, documents: result.documents });
      if (result.added) setNotice(`${result.added} ${result.added === 1 ? 'file' : 'files'} indexed. You can now ask questions.`);
      if (result.errors.length) setError(result.errors.map(item => `${item.name}: ${item.detail}`).join('\n'));
    });
  }

  function remove(doc: DocumentRecord) {
    if (!window.confirm(`Remove "${doc.name}" and clear this conversation?`)) return;
    void perform('Removing file…', async () => {
      await request<void>(`/documents/${doc.id}`, { method: 'DELETE' });
      setWorkspace(current => current && { ...current, documents: current.documents.filter(item => item.id !== doc.id), messages: [] });
      if (scope === doc.id) setScope('');
      setNotice('File removed and conversation cleared.');
    });
  }

  function ask(event: FormEvent) {
    event.preventDefault();
    const question = draft.trim();
    if (!question || !docs.length) return;
    void perform('Searching your sources…', async () => {
      const result = await request<{ messages: Message[] }>('/chat', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, document_ids: scope ? [scope] : [] }),
      });
      setWorkspace(current => current && { ...current, messages: [...current.messages, ...result.messages].slice(-100) });
      setDraft('');
    });
  }

  function clearChat() {
    if (!window.confirm('Clear this conversation? Your files will stay indexed.')) return;
    void perform('Clearing conversation…', async () => {
      await request<void>('/chat', { method: 'DELETE' });
      setWorkspace(current => current && { ...current, messages: [] });
    });
  }

  return <div className="shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-icon"><Sparkles size={21} /></span><div>Marketing AI<small>DOCUMENT WORKSPACE</small></div></div>
      <p className="nav-label">WORKSPACE</p>
      <nav aria-label="Workspace">
        <button className={tab === 'library' ? 'nav-button active' : 'nav-button'} aria-current={tab === 'library' ? 'page' : undefined} onClick={() => setTab('library')}><FolderOpen size={18} /> Source library <span>{docs.length}</span></button>
        <button className={tab === 'chat' ? 'nav-button active' : 'nav-button'} aria-current={tab === 'chat' ? 'page' : undefined} onClick={() => setTab('chat')}><MessageSquare size={18} /> Assistant</button>
      </nav>
      <div className="sidebar-footer"><span className="dot" /> Local workspace</div>
    </aside>

    <main>
      <header className="topbar"><span>Workspace / {tab === 'library' ? 'Source library' : 'Assistant'}</span><span className="pill">{workspace?.answer_mode === 'local-model' ? 'Local model' : 'Local document search'}</span></header>
      <div className="content">
        <section className="hero">
          <div className="kicker"><Sparkles size={14} /> MARKETING DOCUMENT INTELLIGENCE</div>
          <h1>{tab === 'library' ? 'Your campaign knowledge, organized.' : 'Ask about your campaign files.'}</h1>
          <p>{tab === 'library' ? 'Upload briefs, decks, research, and reports. We extract searchable text, save file details, and link each answer back to its source.' : 'Ask about a campaign, audience, channel, or metric. Then follow up without starting over.'}</p>
          {tab === 'library' && docs.length > 0 && <button className="primary" onClick={() => setTab('chat')}>Open assistant <ArrowRight size={16} /></button>}
        </section>

        {(loading || busy || notice) && <p className="status" role="status">{loading ? 'Loading workspace…' : busy || notice}</p>}
        {error && <div className="error" role="alert">{error}{!workspace && <button className="secondary" onClick={() => void load()}>Retry</button>}</div>}

        {tab === 'library' ? <>
          <section aria-labelledby="upload-title" className={`upload${dragging ? ' dragging' : ''}`}
            onDragOver={event => { event.preventDefault(); if (!disabled) setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={event => { event.preventDefault(); setDragging(false); if (!disabled) upload(event.dataTransfer.files); }}>
            <span className="upload-icon"><ArrowDownToLine size={23} /></span>
            <h2 id="upload-title">Upload marketing files</h2>
            <p>Drag files here, or choose them from your computer.</p>
            <input ref={input} type="file" multiple accept={ACCEPT} hidden onChange={event => {
              if (event.currentTarget.files) upload(event.currentTarget.files);
              event.currentTarget.value = '';
            }} />
            <button className="primary" disabled={disabled} onClick={() => input.current?.click()}>Choose files</button>
            <small>PDF, Word, PowerPoint, Excel, CSV, TXT, Markdown, JSON &middot; Up to 5 files per upload, 12 MB each</small>
          </section>
          <section aria-labelledby="library-title" className="library">
            <div className="section-head"><h2 id="library-title">Source library</h2><span className="pill">{docs.length} {docs.length === 1 ? 'file' : 'files'}</span></div>
            {docs.length ? docs.map(doc => <SourceCard key={doc.id} document={doc} disabled={disabled} onRemove={() => remove(doc)} />)
              : <div className="empty"><BookOpen size={21} /><p>No sources yet. Upload a brief or report to get started.</p></div>}
          </section>
        </> : <section className="chat" aria-labelledby="chat-title">
          <div className="section-head chat-head"><h2 id="chat-title">Conversation</h2>
            <button className="secondary" disabled={disabled || !messages.length} onClick={clearChat}><RotateCcw size={14} /> Clear chat</button>
          </div>
          <div className="messages">
            {!messages.length && <div className="chat-empty"><span className="upload-icon"><Sparkles size={22} /></span>
              <h3>{docs.length ? 'Start with a question' : 'Add a source first'}</h3>
              {docs.length ? <div className="prompts">{PROMPTS.map(prompt => <button className="secondary" key={prompt} disabled={disabled} onClick={() => { setDraft(prompt); composer.current?.focus(); }}>{prompt}</button>)}</div>
                : <button className="secondary" onClick={() => setTab('library')}>Go to source library <ArrowRight size={14} /></button>}
            </div>}
            {messages.map(message => <article key={message.id} className={`message ${message.role}`}>
              <p className="message-label">{message.role === 'user' ? 'You' : 'Marketing AI'}</p>
              <p className="message-text">{message.text}</p>
              {message.sources.length > 0 && <div className="citations">{message.sources.map(source => <details key={source.number}>
                <summary>[{source.number}] {source.name} &middot; {source.location}</summary><blockquote>{source.excerpt}</blockquote>
              </details>)}</div>}
            </article>)}
            <div ref={chatEnd} />
          </div>
          <form onSubmit={ask} className="composer">
            <label className="scope">Search in <select value={scope} disabled={disabled || !docs.length} onChange={event => setScope(event.target.value)}>
              <option value="">All uploaded files</option>
              {docs.map(doc => <option key={doc.id} value={doc.id}>{doc.name}</option>)}
            </select></label>
            <div className="input-row">
              <textarea ref={composer} value={draft} maxLength={2000} rows={2} placeholder="Ask a question about your sources…" aria-label="Question"
                disabled={!docs.length || !workspace} onChange={event => setDraft(event.target.value)}
                onKeyDown={event => {
                  if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    if (!disabled) event.currentTarget.form?.requestSubmit();
                  }
                }} />
              <button type="submit" className="primary send" disabled={disabled || !draft.trim() || !docs.length} aria-label="Send question"><ArrowUp size={19} /></button>
            </div>
            <small>{workspace?.answer_mode === 'local-model' ? 'Answers use a local model. Check the cited passages.' : 'Excerpt mode shows relevant text from your files. Enable a local model for synthesized answers.'}</small>
          </form>
        </section>}
        <footer><CheckCircle2 size={14} /> Extracted text, metadata, and chat are stored on this machine.</footer>
      </div>
    </main>
  </div>;
}
