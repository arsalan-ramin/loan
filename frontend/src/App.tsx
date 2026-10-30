import { useCallback, useEffect, useRef, useState } from 'react';
import { Activity, ArrowDownToLine, ArrowRight, ArrowUp, BookOpen, Check, CheckCheck, ChevronDown, ChevronRight, CircleCheck, Clock3, Database, FileCheck2, FileSearch, FileText, FolderOpen, Layers3, ListChecks, LoaderCircle, MessageSquare, Paperclip, Plus, RefreshCw, Settings2, ShieldCheck, Sparkles, Trash2, Upload, X, AlertCircle } from 'lucide-react';
import { api, downloadReport, downloadSourceDocument, downloadUnderwriting } from './api';
import type { UnderwritingFormat } from './api';
import RulesDialog from './RulesDialog';
import PolicyRulesDialog from './PolicyRulesDialog';
import PolicyRouteCard from './PolicyRouteCard';
import UnderwritingReport from './UnderwritingReport';
import GuidedAssistant from './GuidedAssistant';
import ClaudeExplanationText from './ClaudeExplanationText';
import AssistantMessage from './AssistantMessage';
import BatchReview from './BatchReview';
import type { Document, Mode, Session, StoredDataset } from './types';

const statusLabels: Record<Session['status'], string> = { awaiting_upload: 'Awaiting documents', ready: 'Ready to review', processing: 'Processing', completed: 'Review complete', failed: 'Review failed' };
const formatSize = (size: number) => size < 1024 ? `${size} B` : size < 1024 * 1024 ? `${(size / 1024).toFixed(1)} KB` : `${(size / 1024 / 1024).toFixed(1)} MB`;
const readable = (value: string) => value.replace(/[_-]/g, ' ');
const errorText = (cause: unknown) => cause instanceof Error ? cause.message : 'Something went wrong. Please try again.';
const dateLabel = (value: string) => new Date(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });

function isMtbSession(session: Session | null): boolean {
  return !!session?.policy_rules?.length || !!session?.preflight?.recognized_mtb || !!session?.report?.underwriting || !!session?.report?.policy_evaluation || !!session?.documents.some((doc) => /^MTB[ _-]+Query[ _-]+(1|2|3|4)(?=$|[ _().-]).*\.xlsx$/i.test(doc.name));
}

function isStoredDataset(value: unknown): value is StoredDataset {
  const candidate = value as StoredDataset | null;
  return !!candidate && typeof candidate === 'object' && typeof candidate.loan_count === 'number'
    && Array.isArray(candidate.files) && ['empty', 'indexing', 'ready', 'failed', 'stale'].includes(candidate.status);
}

export default function App() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [pollError, setPollError] = useState('');
  const [connection, setConnection] = useState<'checking' | 'online' | 'offline'>('checking');
  const [message, setMessage] = useState('');
  const [chatLoanId, setChatLoanId] = useState<string | undefined>();
  const [chatError, setChatError] = useState('');
  const [pendingQuestion, setPendingQuestion] = useState('');
  const [latestReply, setLatestReply] = useState('');
  const [dragging, setDragging] = useState(false);
  const [rulesOpen, setRulesOpen] = useState(false);
  const [policyRulesOpen, setPolicyRulesOpen] = useState(false);
  const [tab, setTab] = useState<'workspace' | 'report' | 'batch'>('workspace');
  const [mobileNav, setMobileNav] = useState(false);
  const [stored, setStored] = useState<StoredDataset | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const composerInput = useRef<HTMLTextAreaElement>(null);
  const currentId = useRef<string | null>(null);
  const locked = useRef(false);
  const mounted = useRef(true);
  const processing = session?.status === 'processing';
  const disabled = !!busy || !!processing || loading;
  const documents = session?.documents ?? [];
  const needsWorkbookValidation = documents.some((document) => document.extension === '.xlsx')
    && (!session?.selected_loan_id || session.preflight?.scoped_loan_id !== session.selected_loan_id);
  const queryFileSetValid = !!session?.preflight?.query_file_set_valid;
  const enabledRules = session?.rules.filter((rule) => rule.enabled).length ?? 0;
  const isMtb = isMtbSession(session);
  const policyRules = session?.policy_rules ?? [];
  const policyCounts = {
    enforced: policyRules.filter((rule) => rule.state === 'enforced').length,
    conditional: policyRules.filter((rule) => rule.state === 'conditional').length,
    blocked: policyRules.filter((rule) => rule.state === 'blocked').length,
  };

  const syncSession = useCallback((next: Session) => {
    if (!mounted.current) return;
    setSessions((items) => [next, ...items.filter((item) => item.id !== next.id)].sort((a, b) => b.updated_at.localeCompare(a.updated_at)));
    if (currentId.current === next.id) setSession(next);
  }, []);

  const refreshStored = useCallback(async () => {
    try {
      const next = await api.storedDataset();
      if (mounted.current) setStored(isStoredDataset(next) ? next : null);
    } catch {
      if (mounted.current) setStored(null);
    }
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    void refreshStored();
    const [, list] = await Promise.allSettled([
      api.health().then((health) => { if (mounted.current) setConnection(health.status.toLowerCase() === 'error' ? 'offline' : 'online'); }).catch(() => { if (mounted.current) setConnection('offline'); }),
      api.sessions(),
    ]);
    if (!mounted.current) return;
    if (list.status === 'fulfilled') setSessions(list.value.sort((a, b) => b.updated_at.localeCompare(a.updated_at)));
    else setError(errorText(list.reason));
    setLoading(false);
  }, [refreshStored]);

  useEffect(() => {
    if (stored?.status !== 'indexing') return;
    const timer = setTimeout(() => void refreshStored(), 2000);
    return () => clearTimeout(timer);
  }, [stored, refreshStored]);

  useEffect(() => {
    mounted.current = true;
    void load();
    return () => { mounted.current = false; };
  }, [load]);

  useEffect(() => {
    if (!session || session.status !== 'processing') { setPollError(''); return; }
    const id = session.id;
    const controller = new AbortController();
    let timeout: ReturnType<typeof setTimeout>;
    let stopped = false;
    async function poll() {
      try {
        const next = await api.session(id, controller.signal);
        if (stopped) return;
        syncSession(next);
        setPollError('');
        setConnection('online');
        if (next.status !== 'processing') return;
      } catch (cause) {
        if (stopped) return;
        setPollError(`${errorText(cause)} Reconnecting automatically…`);
      }
      if (!stopped) timeout = setTimeout(poll, 2000);
    }
    timeout = setTimeout(poll, 1200);
    return () => { stopped = true; clearTimeout(timeout); controller.abort(); };
  }, [session?.id, session?.status, syncSession]);

  useEffect(() => { setChatLoanId(undefined); setChatError(''); setLatestReply(''); }, [session?.id, session?.report?.id]);

  async function action(label: string, work: () => Promise<void>) {
    if (locked.current) return;
    locked.current = true;
    setBusy(label);
    setError('');
    try { await work(); }
    catch (cause) { if (mounted.current) setError(errorText(cause)); }
    finally { locked.current = false; if (mounted.current) setBusy(null); }
  }

  async function ensureSession(): Promise<Session> {
    if (session) return session;
    const next = await api.create();
    currentId.current = next.id;
    syncSession(next);
    return next;
  }

  async function selectSession(id: string) {
    if (disabled) return;
    await action('Opening session', async () => {
      const next = await api.session(id);
      currentId.current = next.id;
      syncSession(next);
      setMessage(''); setTab('workspace'); setMobileNav(false); setPollError('');
    });
  }

  async function newSession() {
    if (disabled) return;
    await action('Creating session', async () => {
      const next = await api.create();
      currentId.current = next.id;
      syncSession(next);
      setMessage(''); setTab('workspace'); setMobileNav(false); setPollError('');
    });
  }

  async function upload(files: File[]) {
    if (!files.length || disabled) return;
    const hasWorkbook = files.some((file) => file.name.toLowerCase().endsWith('.xlsx'))
      || documents.some((document) => document.extension === '.xlsx');
    const totalWorkbooks = files.filter((file) => file.name.toLowerCase().endsWith('.xlsx')).length
      + documents.filter((document) => document.extension === '.xlsx').length;
    if (hasWorkbook && (totalWorkbooks > 3 || files.some((file) => !file.name.toLowerCase().endsWith('.xlsx'))
      || documents.some((document) => document.extension !== '.xlsx'))) {
      setError('An MTB review uses exactly three XLSX source files: Query 1, Query 2 and Query 4. Other files cannot be mixed in.');
      return;
    }
    setTab('workspace');
    await action('Uploading documents', async () => {
      const active = await ensureSession();
      const uploaded = await api.upload(active.id, files);
      syncSession(uploaded);
      // A complete Query 1, 2 and 4 set is stored and indexed in the background.
      if (uploaded.documents.filter((document) => document.extension === '.xlsx').length === 3) {
        void refreshStored();
        setTimeout(() => void refreshStored(), 1500);
      }
    });
  }

  async function useStoredData() {
    if (disabled) return;
    await action('Loading stored loan data', async () => {
      let active = session;
      if (!active || active.documents.length) {
        active = await api.create();
        currentId.current = active.id;
        syncSession(active);
        setMessage(''); setTab('workspace'); setPollError('');
      }
      syncSession(await api.useStoredDataset(active.id));
    });
  }

  async function send(event: React.FormEvent) {
    event.preventDefault();
    const content = message.trim();
    if (!content || disabled) return;
    setChatError(''); setLatestReply(''); setPendingQuestion(content);
    await action('Sending message', async () => {
      try {
        const active = await ensureSession();
        const next = await api.message(active.id, content, chatLoanId);
        syncSession(next);
        setLatestReply([...next.messages].reverse().find((item) => item.role === 'assistant')?.content ?? 'No assistant reply was returned.');
        setMessage('');
      } catch (cause) {
        setChatError(errorText(cause));
      } finally {
        setPendingQuestion('');
      }
    });
  }

  async function process() {
    if (!session || !documents.length || disabled) return;
    await action('Starting review', async () => { syncSession(await api.process(session.id)); });
  }

  async function openRules() {
    if (disabled) return;
    await action('Opening rules', async () => { await ensureSession(); setRulesOpen(true); });
  }

  return (
    <div className="app-shell">
      {mobileNav && <button className="nav-backdrop" aria-label="Close navigation" onClick={() => setMobileNav(false)} />}
      <aside className={`sidebar ${mobileNav ? 'is-open' : ''}`}>
        <a className="brand" href="#" onClick={(event) => { event.preventDefault(); setTab('workspace'); }}><span className="brand-mark"><Layers3 size={25} strokeWidth={1.7} /></span>agent<span className="brand-dot">.</span></a>
        <div className="workspace-switch"><span className="workspace-avatar">F</span><div>Personal workspace<small>Your document desk</small></div><ChevronDown size={15} /></div>
        <div className="nav-label">WORKSPACE</div>
        <button className={`nav-item ${tab === 'workspace' ? 'active' : ''}`} onClick={() => { setTab('workspace'); setMobileNav(false); }}><FolderOpen size={19} />Document studio<span className="nav-active-dot" /></button>
        <button className={`nav-item ${tab === 'report' ? 'active' : ''}`} onClick={() => { setTab('report'); setMobileNav(false); }}><FileCheck2 size={19} />Review report{session?.report && <span className="tiny-count">1</span>}</button>
        <button className={`nav-item ${tab === 'batch' ? 'active' : ''}`} onClick={() => { setTab('batch'); setMobileNav(false); }}><ListChecks size={19} />Batch review</button>
        <div className="sessions-heading"><span className="nav-label">YOUR SESSIONS</span><button className="sidebar-icon" title="New session" aria-label="New session" disabled={disabled} onClick={() => void newSession()}><Plus size={17} /></button></div>
        <div className="session-list">
          {loading && <p className="sidebar-empty">Loading your sessions…</p>}
          {!loading && !sessions.length && <div className="sidebar-empty"><MessageSquare size={20} /><p>A clean slate.<br />Your sessions will live here.</p></div>}
          {sessions.map((item) => <button key={item.id} disabled={disabled} className={`session-item ${session?.id === item.id ? 'selected' : ''}`} onClick={() => void selectSession(item.id)}><MessageSquare size={15} /><span>{item.title || 'Untitled session'}<small>{dateLabel(item.updated_at)}<span className={`session-status ${item.status}`} /></small></span><ChevronRight size={13} /></button>)}
        </div>
        <div className="sidebar-bottom"><div className="private-note"><ShieldCheck size={18} /><div>A little more clarity.<br /><span>A lot less busywork.</span></div></div><div className="profile"><span className="profile-avatar">Y</span><div>Your workspace<small>Local demo · no sign-in</small></div><span className={`connection-dot ${connection}`} title={`API ${connection}`} /></div></div>
      </aside>

      <div className="main-shell">
        <header className="topbar"><div className="breadcrumb"><button className="icon-button mobile-menu" aria-label="Open navigation" onClick={() => setMobileNav(true)}><Layers3 size={21} /></button><span>Workspace</span><ChevronRight size={14} /><strong>{tab === 'workspace' ? 'Document studio' : tab === 'batch' ? 'Batch review' : 'Review report'}</strong></div><div className="topbar-right"><span className="local-badge"><span className={`connection-dot ${connection}`} />{connection === 'online' ? 'Connected to your API' : connection === 'offline' ? 'API offline' : 'Connecting to API'}</span><span className="top-avatar">Y</span></div></header>

        <main>
          <section className="page-heading"><div><div className="eyebrow"><span />DOCUMENT INTELLIGENCE, SIMPLIFIED</div><h1>{tab === 'workspace' ? 'Good documents. Clear decisions.' : tab === 'batch' ? 'Every loan. One run.' : 'The details. The bigger picture.'}</h1><p>{tab === 'workspace' ? 'From a stack of files to a clear next step. Let’s make sense of it.' : tab === 'batch' ? 'Review the whole stored loan set, track progress, and export the results.' : 'A transparent review, grounded in your documents and your rules.'}</p></div><button className="button secondary new-session" disabled={disabled} onClick={() => void newSession()}><Plus size={17} />New session</button></section>

          {error && <div className="error-banner" role="alert"><AlertCircle size={19} /><span>{error}</span><button className="text-button" disabled={!!busy} onClick={() => void load()}><RefreshCw size={14} />Reconnect</button><button className="icon-button" aria-label="Dismiss error" onClick={() => setError('')}><X size={17} /></button></div>}
          {pollError && <div className="error-banner" role="status"><RefreshCw size={17} /><span>{pollError}</span></div>}

          {tab === 'batch' ? <BatchReview /> : <>

          <div className="workspace-summary"><div className="session-title"><span className="icon-tile small"><FolderOpen size={18} /></span><div><h2>{session?.title || 'Your next great review'}</h2><span>{session ? `Created ${dateLabel(session.created_at)}` : 'A fresh space for your documents'}</span></div></div><span className={`status-pill ${session?.status || 'awaiting_upload'}`}><span />{statusLabels[session?.status || 'awaiting_upload']}</span></div>

          {isMtb && session && <section className="underwriting-mode" aria-label="Underwriting mode">
            <label htmlFor="underwriting-mode">Review mode</label>
            <select id="underwriting-mode" value={session.mode ?? 'TEST'} disabled={disabled} aria-describedby="mode-help" onChange={(event) => {
              const mode = event.target.value as Mode;
              if (disabled || mode === (session.mode ?? 'TEST')) return;
              void action('Updating review mode', async () => { syncSession(await api.mode(session.id, mode)); });
            }}>
              <option value="PRODUCTION">PRODUCTION — manual review only</option>
              <option value="TEST">TEST — simulated paths only</option>
            </select>
            <p id="mode-help">TEST produces simulated paths with no operational action. PRODUCTION requires manual review, not an automated lending decision. Changing mode clears the current report; process documents again.</p>
          </section>}

          {tab === 'report' ? <Report session={session} busy={disabled} onDownload={() => session && void action('Downloading report', () => downloadReport(session.id))} onExport={(format) => session && void action(`Downloading ${format.toUpperCase()}`, () => downloadUnderwriting(session.id, format))} onBack={() => setTab('workspace')} /> : <>
            <div className="studio-layout">
              <section className="card documents-card"><div className="card-heading"><div className="heading-with-icon"><FilesIcon /><h2>Documents</h2><span className="count-badge">{documents.length}</span></div><button className="icon-button" aria-label="Add documents" disabled={disabled} onClick={() => fileInput.current?.click()}><Plus size={18} /></button></div>
                <div className="documents-content">
                  <div className="upload-controls">
                    <div role="button" tabIndex={disabled ? -1 : 0} aria-disabled={disabled} aria-label="Upload documents" className={`dropzone ${dragging ? 'dragging' : ''} ${disabled ? 'disabled' : ''}`} onClick={() => !disabled && fileInput.current?.click()} onKeyDown={(event) => { if ((event.key === 'Enter' || event.key === ' ') && !disabled) { event.preventDefault(); fileInput.current?.click(); } }} onDragOver={(event) => { event.preventDefault(); if (!disabled) setDragging(true); }} onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false); }} onDrop={(event) => { event.preventDefault(); setDragging(false); void upload(Array.from(event.dataTransfer.files)); }}><span className="upload-icon">{busy === 'Uploading documents' ? <LoaderCircle className="spin" size={23} /> : <Upload size={23} strokeWidth={1.6} />}</span><strong>{busy === 'Uploading documents' ? 'Adding your documents…' : dragging ? 'Drop files to add them' : 'Drop your documents here'}</strong><span>or <b>browse files</b> to get started</span><small>No application file-size or record limits · Large files may take longer</small></div>
                    <input ref={fileInput} className="sr-only" type="file" accept=".pdf,.docx,.xlsx,.txt,.md,.csv,.json" multiple aria-label="Choose documents" disabled={disabled} onChange={(event) => { void upload(Array.from(event.target.files || [])); event.target.value = ''; }} />
                  </div>
                  <div className="upload-notices">
                    {stored && stored.status !== 'empty' && <div className="stored-dataset">
                      {stored.status === 'ready' ? <>
                        <button className="button secondary dataset-button" disabled={disabled} onClick={() => void useStoredData()}><Database size={16} />Use stored loan data{documents.length ? ' in a new session' : ''}</button>
                        <p className="dataset-note">{stored.loan_count.toLocaleString()} loans and {stored.account_count.toLocaleString()} borrower accounts are stored and indexed{stored.updated_at ? ` (updated ${dateLabel(stored.updated_at)})` : ''}, so no upload is needed. Uploading a new Query 1, 2 and 4 set clears and replaces all stored data.</p>
                      </> : <p className="dataset-note" role="status">{stored.status === 'indexing'
                        ? <><LoaderCircle size={13} className="spin" /> Clearing previous stored data and indexing the uploaded Query 1, 2 and 4 workbooks by loan and account…</>
                        : stored.status === 'stale' ? 'Stored loan data was indexed with older mappings. Upload Query 1, 2 and 4 again to rebuild it.'
                          : stored.error || 'Stored loan data could not be indexed. Upload Query 1, 2 and 4 again.'}</p>}
                    </div>}
                    <p className="dataset-note">Upload the three MTB source workbooks together: Query 1 (Loans, Accounts & Financials), Query 2 (SBSS & Entity Relationships), and Query 4 (Credit Reports). Then enter a loan number to load only its related rows through explicit IDs; no row-position or name joins are used. Excel .xls and .xlsm are unsupported.</p>
                    <p className="dataset-note">V4 guideline available. Amounts use actual USD dollars; source currency and scale must match. Policy conflicts, production signoff and missing field mappings still require review. Generic document checks cannot approve financing.</p>
                  </div>
                  {documents.length ? <ul className="document-list">{documents.map((doc) => <li key={doc.id}><span className="document-icon"><FileText size={19} /></span><div><strong title={doc.name}>{doc.name}</strong><small>{doc.extension.replace('.', '').toUpperCase() || 'FILE'}<span>·</span>{formatSize(doc.size)}</small><ExcelMetadata document={doc} /></div><button className="icon-button" disabled={disabled} aria-label={`Download ${doc.name}`} title={`Download ${doc.name}`} onClick={() => void action('Downloading source document', () => downloadSourceDocument(session!.id, doc.id, doc.name))}><ArrowDownToLine size={15} /></button><button className="icon-button danger" disabled={disabled} aria-label={`Remove ${doc.name}`} onClick={() => void action('Removing document', async () => { syncSession(await api.remove(session!.id, doc.id)); })}><Trash2 size={15} /></button></li>)}</ul> : <div className="no-documents"><FileText size={14} />No documents added yet</div>}
                </div>
              </section>
              <section className="card conversation-card">
                <div className="card-heading"><div className="heading-with-icon"><MessageSquare size={19} /><h2>Your document conversation</h2></div><span className="subtle-label"><span className="green-dot" />Agent assistant</span></div>
                <div className="conversation-body" aria-live="polite" aria-label="Conversation">
                  {!session?.messages.length && <div className="welcome-message"><div className="assistant-avatar"><Sparkles size={19} /></div><div><div className="message-author">Agent<span>Your document copilot</span></div><div className="assistant-bubble"><h3>A fresh perspective starts here.</h3><p>Upload your documents and I’ll help you see what matters. Together, we can check the details, spot gaps, and turn your files into a clear, actionable report.</p><div className="upload-guidance"><Paperclip size={16} /><span>Start by adding your first document above.</span></div></div></div></div>}
                  {session?.messages.filter((item, index) => !(latestReply && index === session.messages.length - 1 && item.role === 'assistant' && item.content === latestReply)).map((item) => <div key={item.id} className={`chat-message ${item.role}`}><div className={item.role === 'assistant' ? 'assistant-avatar' : 'user-avatar'}>{item.role === 'assistant' ? <Sparkles size={17} /> : 'Y'}</div><div><div className="message-author">{item.role === 'assistant' ? 'Agent' : 'You'}<time>{new Date(item.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time></div><div className={item.role === 'assistant' ? 'assistant-bubble message-content' : 'user-bubble message-content'}>{item.role === 'assistant' ? <AssistantMessage content={item.content} /> : item.content}</div></div></div>)}
                  {(processing || session?.status === 'completed') && <div tabIndex={-1} aria-label="Processing explanation result" aria-live="polite">
                    {processing ? <section className="claude-explanation" role="status"><h3>Claude explanation</h3><p>Review in progress. Claude’s explanation will appear here when processing completes.</p><p className="muted">Processing includes billable Anthropic access. No explanation is available yet.</p></section>
                      : session?.report && !session.report.underwriting && !(isMtb && !session.report.policy_evaluation) && <ClaudeExplanation report={session.report} showUnavailable />}
                  </div>}
                  {session && <GuidedAssistant session={session} disabled={disabled} run={action} onSession={syncSession}
                    onUpload={() => fileInput.current?.click()} onReport={() => setTab('report')}
                    onDownload={() => void action('Downloading report', () => downloadReport(session.id))}
                    onAsk={(loanId, question) => {
                      setChatLoanId(loanId); setMessage(question);
                      composerInput.current?.focus({ preventScroll: true });
                    }} />}
                  {session?.report?.underwriting && <section className="aggregate-commentary" aria-label="Aggregate report commentary"><h3>Aggregate report commentary</h3><ClaudeExplanation report={session.report} showUnavailable /></section>}
                  {!documents.length && !session?.messages.length && <div className="conversation-empty"><div className="paper-art"><FileText size={39} strokeWidth={1.25} /><span><Sparkles size={16} /></span></div><p>A little context. A lot of possibility.</p><span>Your documents are the starting point.</span></div>}
                  {busy === 'Sending message' && <div className="thinking"><LoaderCircle size={15} className="spin" />Agent is thinking…</div>}
                </div>
                <div className="chat-feedback" aria-live="polite">
                  {pendingQuestion && <section role="status"><h3>Sending your follow-up…</h3><p>{pendingQuestion}</p><p>Waiting for Anthropic. No answer is available yet.</p></section>}
                  {chatError && <section role="alert"><h3>Your follow-up could not be answered</h3><p>{chatError}</p><p>Your question is still in Message Agent. Edit it or press Send to retry; another request may incur charges. Your saved report has not changed.</p><button type="button" className="text-button" onClick={() => { setChatError(''); composerInput.current?.focus({ preventScroll: true }); }}>Edit question</button></section>}
                  {latestReply && <section aria-label="Latest agent reply"><h3>Agent’s reply</h3><AssistantMessage content={latestReply} /></section>}
                </div>
                <div className="composer-area">{chatLoanId && <p>Focused report context: loan {chatLoanId}. Session history and recall may include other session documents. <button type="button" className="text-button" disabled={disabled} onClick={() => setChatLoanId(undefined)}>Clear loan focus</button></p>}<form onSubmit={send} className="composer"><button className="icon-button" type="button" aria-label="Attach documents" disabled={disabled} onClick={() => fileInput.current?.click()}><Paperclip size={20} /></button><textarea ref={composerInput} aria-label="Message Agent" rows={1} value={message} onChange={(event) => setMessage(event.target.value)} disabled={disabled} placeholder={!documents.length ? 'Ask a question, or upload your documents to begin…' : 'Ask about documents, rules, or earlier messages…'} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(event); } }} /><button type="submit" className="send-button" aria-label="Send message" disabled={disabled || !message.trim()}>{busy === 'Sending message' ? <LoaderCircle size={18} className="spin" /> : <ArrowUp size={19} />}</button></form><div className="composer-note"><ShieldCheck size={12} /><span>Grounded in your documents. Guided by your rules.</span><span className="enter-hint">Enter to send</span></div><p className="privacy-disclosure">Chat context and document evidence are sent to Anthropic for chat and Claude report explanations. Requires a backend API key and billable network access. Storage and lexical embeddings stay local.</p></div>
              </section>

              <div className="rules-row">
                {isMtb ? <>
                  <section className="card rules-card" aria-labelledby="underwriting-rules-heading">
                    <div className="card-heading"><div className="heading-with-icon"><ShieldCheck size={19} /><h2 id="underwriting-rules-heading">Underwriting rules</h2></div></div>
                    <div className="policy-rules-body">
                      <ul className="policy-counts"><li>{policyCounts.enforced} enforced safeguards</li><li>{policyCounts.conditional} conditional checks</li><li>{policyCounts.blocked} blocked checks</li></ul>
                      <p>Draft guideline, not approved credit policy. No lending decisions; authorized human review required.</p>
                      <p>Conditional checks require a matching product and valid evidence, not a claim of evaluation. Blocked checks await confirmation of requirements such as units or thresholds.</p>
                      {!policyRules.length && <p>No guideline rules were supplied by the server.</p>}
                      <button className="text-button" disabled={disabled} onClick={() => setPolicyRulesOpen(true)}>View guideline rules<ArrowRight size={15} /></button>
                    </div>
                  </section>
                  <section className="card rules-card" aria-labelledby="document-checks-heading">
                    <div className="card-heading"><div className="heading-with-icon"><Settings2 size={19} /><h2 id="document-checks-heading">Optional document checks</h2></div></div>
                    <div className="policy-rules-body">
                      <p>Custom document validation only; separate from underwriting rules.</p>
                      {!!session?.rules.length && <p>{enabledRules} enabled document checks · {session.rules.length} configured</p>}
                      <button className="text-button" disabled={disabled} onClick={() => void openRules()}>Edit document checks<ArrowRight size={15} /></button>
                    </div>
                  </section>
                </> : <section className="card rules-card"><div className="card-heading"><div className="heading-with-icon"><ShieldCheck size={19} /><h2>Review rules</h2></div><button className="icon-button" aria-label="Configure review rules" disabled={disabled} onClick={() => void openRules()}><Settings2 size={17} /></button></div><div className="rules-card-body"><div><span className="rule-summary-icon"><CheckCheck size={20} /></span><div><strong>{enabledRules ? `${enabledRules} active ${enabledRules === 1 ? 'rule' : 'rules'}` : 'Make the review your own'}</strong><p>{enabledRules ? 'Your standards, applied consistently.' : 'Define what a good document looks like.'}</p></div></div><button className="text-button" disabled={disabled} onClick={() => void openRules()}>{session?.rules.length ? 'Manage rules' : 'Configure rules'}<ArrowRight size={15} /></button></div></section>}
              </div>
            </div>

            <section className="card pipeline-card"><div className="pipeline-header"><div><div className="heading-with-icon"><Activity size={19} /><h2>A little teamwork. A thorough review.</h2></div><p>Your agent pipeline, from first read to final recommendation.</p></div><span className="pipeline-label">{processing ? <><LoaderCircle size={13} className="spin" />Review in progress</> : session?.steps.length ? `${session.steps.filter((step) => step.status === 'completed').length} / ${session.steps.length} steps complete` : <><span className="neutral-dot" />Ready when you are</>}</span></div>
              <div className="pipeline-steps">{session?.steps.length ? session.steps.map((step, index) => <div key={`${step.agent}-${index}`} className={`pipeline-step ${step.status}`}><div className="step-symbol">{step.status === 'running' ? <LoaderCircle size={20} className="spin" /> : step.status === 'completed' ? <Check size={20} /> : step.status === 'failed' ? <X size={20} /> : <Clock3 size={19} />}</div><div><span className="step-kicker">AGENT {String(index + 1).padStart(2, '0')} · {step.status}</span><h3>{readable(step.agent)}</h3><p>{step.detail || 'Waiting for the next step.'}</p></div>{index < session.steps.length - 1 && <ChevronRight className="step-chevron" size={17} />}</div>) : <><PipelinePlaceholder icon={<FileSearch size={21} />} number="01" title="Read & understand" detail="Bring the important details into focus." /><PipelinePlaceholder icon={<ShieldCheck size={21} />} number="02" title="Check & validate" detail="Measure the details against your rules." /><PipelinePlaceholder icon={<FileCheck2 size={21} />} number="03" title="Summarize & report" detail="Turn findings into a clear next step." /></>}</div>
              <div className="pipeline-footer"><div><span className={`footer-status-icon ${processing ? 'working' : ''}`}>{processing ? <LoaderCircle size={17} className="spin" /> : <Sparkles size={17} />}</span><p>{processing ? 'Your agents are on it. This page updates automatically.' : !documents.length ? 'Add your documents. We’ll take it from there.' : session?.status === 'completed' ? 'Your review is ready. Explore the findings in your report.' : needsWorkbookValidation ? 'Enter a loan number to load only its related data.' : isMtb && !queryFileSetValid ? 'Upload one workbook each for Query 1, Query 2, and Query 4.' : 'Everything in place? Let’s connect the dots.'}</p></div><div className="process-actions">{session?.report && <button className="button secondary" onClick={() => setTab('report')}>View report<ArrowRight size={15} /></button>}<button className="button primary" disabled={disabled || !documents.length || needsWorkbookValidation || (isMtb && !queryFileSetValid)} onClick={() => void process()}>{processing || busy === 'Starting review' ? <LoaderCircle size={16} className="spin" /> : <Sparkles size={16} />}{processing ? 'Processing documents' : session?.status === 'failed' ? 'Retry processing' : 'Process documents'}{!processing && <ArrowRight size={16} />}</button></div></div>
            </section>
            {session?.error && <div role="alert" className="error-banner"><AlertCircle size={18} /><span>{session.error}</span></div>}
            {session?.report && <div className="report-ready"><span className="icon-tile"><FileCheck2 size={23} /></span><div><h3>Your review is ready.</h3><p>{session.report.summary}</p></div><button className="button secondary" onClick={() => setTab('report')}>Explore findings<ArrowRight size={16} /></button></div>}
          </>}
          </>}
          <footer className="page-footer"><span className="footer-brand"><Layers3 size={14} />A clearer way forward.</span><span>Thoughtfully reviewed. Always in your control.</span></footer>
        </main>
      </div>
      {rulesOpen && session && <RulesDialog rules={session.rules} documentChecks={isMtb} onClose={() => setRulesOpen(false)} onSave={async (rules) => { setError(''); syncSession(await api.rules(session.id, rules)); }} />}
      {policyRulesOpen && session && <PolicyRulesDialog rules={policyRules} onClose={() => setPolicyRulesOpen(false)} />}
      {busy && <div className="sr-only" role="status">{busy}</div>}
    </div>
  );
}

function ExcelMetadata({ document }: { document: Document }) {
  const { sheets, data_row_count, warning } = document.metadata;
  return <div className="excel-metadata">
    {typeof data_row_count === 'number' && <p>Data rows (excluding README sheets): {data_row_count}</p>}
    {!!sheets?.length && <details><summary>View worksheet columns and details</summary>{sheets.map((sheet, index) => <div className="excel-sheet" key={`${sheet.name}-${index}`}>
      <p>Sheet: {sheet.name}{sheet.is_reference ? ' (reference)' : ''}</p>
      <p>{sheet.columns} columns · {sheet.data_rows} data rows · Header row: {sheet.header_row ?? 'None'}</p>
      {!!sheet.headers.length && <p>Headers: {sheet.headers.join(', ')}</p>}
    </div>)}</details>}
    {warning && <p className="document-warning">{warning}</p>}
  </div>;
}

function FilesIcon() { return <Layers3 size={19} />; }
function PipelinePlaceholder({ icon, number, title, detail }: { icon: React.ReactNode; number: string; title: string; detail: string }) {
  return <div className="pipeline-step placeholder-step"><div className="step-symbol">{icon}</div><div><span className="step-kicker">STEP {number}</span><h3>{title}</h3><p>{detail}</p></div><ChevronRight className="step-chevron" size={17} /></div>;
}

function Report({ session, busy, onDownload, onExport, onBack }: { session: Session | null; busy: boolean; onDownload: () => void; onExport: (format: UnderwritingFormat) => void; onBack: () => void }) {
  const report = session?.report;
  const missingPolicy = isMtbSession(session) && !report?.underwriting && !report?.policy_evaluation;
  const documentChecksOnly = isMtbSession(session);
  if (!report) return <section className="card empty-report"><div className="empty-report-icon"><BookOpen size={37} strokeWidth={1.3} /></div><span className="eyebrow">CLARITY IS ON THE WAY</span><h2>A good review starts with your documents.</h2><p>{session?.status === 'processing' ? 'Your agents are working through the details. Your report will appear here when the review is complete.' : 'Upload your files, set your rules, and process your documents. Your findings and downloadable report will appear here.'}</p><button className="button primary" onClick={onBack}>Back to document studio<ArrowRight size={16} /></button></section>;
  return <section className="card report-card">
    <div className="report-heading"><div><div className="eyebrow">YOUR DOCUMENT REVIEW</div><h2>From information to insight.</h2><span className="muted">{dateLabel(report.created_at)} · {report.document_count} {report.document_count === 1 ? 'document' : 'documents'} reviewed</span></div><button className="button primary" disabled={busy || missingPolicy} onClick={onDownload}><ArrowDownToLine size={16} />Download HTML</button></div>
    {report.underwriting && <div className="underwriting-downloads" aria-label="Underwriting exports">
      <button className="button secondary" disabled={busy || session?.status !== 'completed'} onClick={() => onExport('json')}><ArrowDownToLine size={16} />Download JSON</button>
      <button className="button secondary" disabled={busy || session?.status !== 'completed'} onClick={() => onExport('csv')}><ArrowDownToLine size={16} />Download CSV</button>
    </div>}
    {report.underwriting ? <UnderwritingReport batch={report.underwriting} /> : report.policy_evaluation && <PolicyRouteCard evaluation={report.policy_evaluation} />}
    {missingPolicy && <div className="claude-warning" role="alert"><AlertCircle size={18} /><div><strong>Underwriting evaluation missing</strong><p>This report contains only document checks. Run Process documents again to evaluate the guideline. It is not a lending approval.</p></div></div>}
    <div className={`decision-banner ${documentChecksOnly ? 'document-checks' : report.decision}`}><span className="decision-icon">{documentChecksOnly ? <ShieldCheck size={25} /> : report.decision === 'approved' ? <CircleCheck size={25} /> : <AlertCircle size={25} />}</span><div><h3>{documentChecksOnly ? 'Document checks' : readable(report.decision)}</h3><p>{documentChecksOnly ? 'Document validation findings only; not a financing decision.' : report.summary}</p></div></div>
    <div className="findings-heading"><h3>The findings</h3><span className="muted">{report.findings.length} checks</span></div>
    {!report.findings.length && <div className="no-findings"><ShieldCheck size={25} /><p>No individual rule findings were returned for this review.</p></div>}
    <div className="findings-list">{report.findings.map((finding, index) => <article className={`finding ${finding.status}`} key={`${finding.rule_id}-${index}`}><div className="finding-header"><span className={`finding-status ${finding.status}`}>{finding.status === 'pass' ? <CircleCheck size={15} /> : <AlertCircle size={15} />}{finding.status === 'pass' ? 'Passed' : finding.status === 'review' ? 'Needs review' : 'Failed'}</span><span className="finding-document"><FileText size={13} />{finding.document}</span></div><h4>{finding.rule_name}</h4><p>{finding.detail}</p>{finding.evidence && <blockquote><span>EVIDENCE</span>{finding.evidence}</blockquote>}</article>)}</div>
    {!missingPolicy && <ClaudeExplanation report={report} />}
    <div className="report-footnote"><ShieldCheck size={15} />{documentChecksOnly ? 'Non-operational findings only. Authorized human review required.' : 'Automated findings are a starting point. Use your judgment for the final decision.'}</div>
  </section>;
}

function ClaudeExplanation({ report, showUnavailable = false }: { report: NonNullable<Session['report']>; showUnavailable?: boolean }) {
  if (!showUnavailable && !report.llm_analysis && !report.llm_error && !report.llm_model) return null;
  return <section className="claude-explanation" aria-labelledby="claude-explanation-heading">
      <div className="heading-with-icon"><Sparkles size={19} /><h3 id="claude-explanation-heading">Claude explanation</h3></div>
      <p className="muted">{report.underwriting ? 'Aggregate AI-generated commentary only. Per-loan explanations and rule findings remain deterministic. No operational lending decision; authorized human review required.' : report.policy_evaluation ? 'AI-generated commentary only. Draft routes and document checks are not lending decisions. Authorized review required.' : 'AI-generated commentary only. The deterministic decision and rule findings above remain authoritative.'}</p>
      {report.llm_analysis && <ClaudeExplanationText text={report.llm_analysis} />}
      {report.llm_model && <details><summary>Claude audit details</summary><p className="claude-model">Model: {report.llm_model}</p><p>Saved report: {report.id}</p></details>}
      {!report.llm_analysis && !report.llm_error && <p>No Claude explanation was returned for this saved report. Deterministic findings remain available; no new provider request has been made.</p>}
      {report.llm_error && <div className="claude-warning" role="alert"><AlertCircle size={18} /><div><strong>Claude explanation warning</strong><p>{report.llm_error}</p><p>Your deterministic report and findings remain available for review and download.</p></div></div>}
  </section>;
}