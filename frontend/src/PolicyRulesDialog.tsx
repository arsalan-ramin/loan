import { useEffect, useRef } from 'react';
import { ShieldCheck, X } from 'lucide-react';
import type { PolicyRuleDefinition } from './types';

interface Props {
  rules: PolicyRuleDefinition[];
  onClose: () => void;
}

export default function PolicyRulesDialog({ rules, onClose }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current!;
    element.showModal();
    return () => element.close();
  }, []);

  return (
    <dialog ref={dialog} className="rules-dialog policy-rules-dialog" aria-labelledby="policy-rules-title" onCancel={(event) => { event.preventDefault(); onClose(); }} onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="dialog-heading"><div className="icon-tile"><ShieldCheck size={22} /></div><button className="icon-button" type="button" aria-label="Close guideline rules" onClick={onClose}><X size={20} /></button></div>
      <h2 id="policy-rules-title">Underwriting rules</h2>
      <p className="policy-disclaimer">Draft guideline, not approved credit policy. No lending approval or denial decisions. Authorized human review and policy-owner signoff are required.</p>
      <p className="policy-catalog-note">Read-only catalog supplied by the server. Enforced safeguards are distinct from conditional or blocked checks. Conditional checks apply only to matching products with valid evidence; inclusion here does not mean a check was evaluated. Blocked checks cannot run until their stated requirements are confirmed.</p>
      <div className="policy-catalog-list">
        {!rules.length && <p className="policy-catalog-note">No guideline rules were supplied by the server. No policy checks are claimed evaluated.</p>}
        {rules.map((rule) => <article className="policy-catalog-rule" key={rule.id}>
          <p className="policy-catalog-category">{rule.category} · PDF page {rule.page}</p>
          <h3>{rule.name}</h3>
          <p className={`policy-state ${rule.state}`}>State: {rule.state}</p>
          <p>{rule.description}</p>
          <p><strong>Reason: </strong>{rule.reason}</p>
        </article>)}
      </div>
      <div className="dialog-footer"><button type="button" className="button secondary" onClick={onClose}>Close</button></div>
    </dialog>
  );
}