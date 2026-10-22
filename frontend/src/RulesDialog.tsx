import { useEffect, useRef, useState } from 'react';
import { Plus, ShieldCheck, Trash2, X } from 'lucide-react';
import type { Rule } from './types';

interface Props {
  rules: Rule[];
  documentChecks?: boolean;
  onClose: () => void;
  onSave: (rules: Rule[]) => Promise<void>;
}

export default function RulesDialog({ rules, documentChecks = false, onClose, onSave }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [draft, setDraft] = useState<Rule[]>(() => rules.map((rule) => ({ ...rule })));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    const element = dialog.current!;
    element.showModal();
    return () => element.close();
  }, []);
  function update(id: string, change: Partial<Rule>) {
    setDraft((items) => items.map((rule) => rule.id === id ? { ...rule, ...change } : rule));
    setError('');
  }
  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (draft.some((rule) => !rule.name.trim() || !rule.value.trim() || (rule.kind === 'min_words' && (!/^\d+$/.test(rule.value) || !Number.isSafeInteger(Number(rule.value)) || Number(rule.value) < 1 || Number(rule.value) > 1_000_000)))) {
      setError('Give every rule a name and value. Minimum words must be a whole number from 1 to 1,000,000.');
      return;
    }
    setSaving(true);
    setError('');
    try {
      await onSave(draft.map((rule) => ({ ...rule, name: rule.name.trim(), value: rule.value.trim() })));
      onClose();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not save rules.');
    } finally { setSaving(false); }
  }
  return (
    <dialog ref={dialog} className="rules-dialog" aria-labelledby="rules-title" onCancel={(event) => { event.preventDefault(); if (!saving) onClose(); }} onClick={(event) => { if (event.target === event.currentTarget && !saving) onClose(); }}>
      <form onSubmit={save}>
        <div className="dialog-heading"><div className="icon-tile"><ShieldCheck size={22} /></div><button className="icon-button" type="button" aria-label="Close rules" disabled={saving} onClick={onClose}><X size={20} /></button></div>
        <h2 id="rules-title">{documentChecks ? 'Optional document checks' : 'Your standards. Set in rules.'}</h2>
        <p className="muted">{documentChecks ? 'Custom document validation only, separate from underwriting rules. These checks do not edit the draft guideline or make lending decisions.' : 'Tell Agent what matters. Enabled rules are checked when you process your documents.'}</p>
        <div className="rules-list">
          {!draft.length && <div className="rules-empty"><ShieldCheck size={28} /><h3>A little guidance goes a long way.</h3><p>Add your first rule to make every review consistent.</p></div>}
          {draft.map((rule, index) => <fieldset className="rule-editor" key={rule.id} disabled={saving}>
            <legend>Rule {index + 1}</legend>
            <div className="rule-top"><label className="check-label"><input type="checkbox" checked={rule.enabled} onChange={(event) => update(rule.id, { enabled: event.target.checked })} />Enabled</label><button type="button" className="icon-button danger" aria-label={`Delete rule ${index + 1}`} onClick={() => setDraft(draft.filter((item) => item.id !== rule.id))}><Trash2 size={16} /></button></div>
            <label>Rule name<input required value={rule.name} placeholder="e.g. Must include an effective date" onChange={(event) => update(rule.id, { name: event.target.value })} /></label>
            <div className="rule-columns"><label>Check type<select value={rule.kind} onChange={(event) => update(rule.id, { kind: event.target.value as Rule['kind'] })}><option value="required_text">Required text</option><option value="forbidden_text">Forbidden text</option><option value="min_words">Minimum words</option><option value="required_metadata">Required metadata</option></select></label><label>If not met<select value={rule.severity} onChange={(event) => update(rule.id, { severity: event.target.value as Rule['severity'] })}><option value="review">Needs review</option><option value="fail">Fail check</option></select></label></div>
            <label>{rule.kind === 'min_words' ? 'Minimum word count' : rule.kind === 'required_metadata' ? 'Metadata key' : 'Text to match'}<input required type={rule.kind === 'min_words' ? 'number' : 'text'} min={rule.kind === 'min_words' ? 1 : undefined} max={rule.kind === 'min_words' ? 1000000 : undefined} step={rule.kind === 'min_words' ? 1 : undefined} value={rule.value} placeholder={rule.kind === 'required_metadata' ? 'e.g. author' : rule.kind === 'min_words' ? 'e.g. 250' : 'e.g. effective date'} onChange={(event) => update(rule.id, { value: event.target.value })} /></label>
          </fieldset>)}
        </div>
        <button type="button" className="button secondary add-rule" disabled={saving} onClick={() => setDraft([...draft, { id: crypto.randomUUID(), name: '', kind: 'required_text', value: '', severity: 'review', enabled: true }])}><Plus size={16} />Add a rule</button>
        {error && <p className="inline-error" role="alert">{error}</p>}
        <div className="dialog-footer"><button type="button" className="button secondary" disabled={saving} onClick={onClose}>Cancel</button><button className="button primary" disabled={saving}>{saving ? 'Saving…' : 'Save rules'}</button></div>
      </form>
    </dialog>
  );
}