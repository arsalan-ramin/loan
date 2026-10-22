import type { PolicyEvaluation } from './types';

function DetailList({ title, items }: { title: string; items: string[] }) {
  return <div className="policy-details"><h4>{title}</h4>{items.length
    ? <ul>{items.map((item, index) => <li key={index}>{item}</li>)}</ul>
    : <p>None reported.</p>}</div>;
}

export default function PolicyRouteCard({ evaluation }: { evaluation: PolicyEvaluation }) {
  return <section className="policy-route-card" aria-labelledby="policy-route-heading">
    <span className="eyebrow">DRAFT · NOT APPROVED CREDIT POLICY</span>
    <h3 id="policy-route-heading">Draft underwriting route</h3>
    <p className="policy-route">{evaluation.recommended_route}</p>
    <p className="policy-disclaimer"><strong>Not a lending approval or denial. Authorized review required.</strong></p>
    <dl className="policy-source">
      <div><dt>Source document</dt><dd>{evaluation.source_document}</dd></div>
      <div><dt>Policy version</dt><dd>{evaluation.policy_version}</dd></div>
      <div><dt>SHA-256</dt><dd>{evaluation.source_sha256 ?? 'Not provided'}</dd></div>
      <div><dt>Policy status</dt><dd>{evaluation.policy_status}</dd></div>
    </dl>
    <DetailList title="Missing information / unknowns" items={evaluation.missing_information} />
    <h4 className="policy-section-heading">Draft rule results</h4>
    {!evaluation.rule_results.length && <p>No rule results reported.</p>}
    {evaluation.rule_results.map((rule, index) => <article className="policy-result" key={`${rule.rule_id}-${index}`}>
      <h4>{rule.rule_id} · {rule.page === null ? 'Data diagnostic — no PDF citation' : `PDF page ${rule.page}`}</h4>
      <p className={`policy-rule-status ${rule.status}`}>Status: {rule.status === 'not_triggered' ? 'Not triggered' : rule.status === 'triggered' ? 'Triggered' : 'Unknown'}</p>
      <p>Route: {rule.route ?? 'Undetermined'}</p>
      <p>{rule.detail}</p>
      <DetailList title="Evidence" items={rule.evidence} />
    </article>)}
    <h4 className="policy-section-heading">Profiles</h4>
    {!evaluation.profiles.length && <p>No profiles reported.</p>}
    {evaluation.profiles.map((profile, index) => <article className="policy-result" key={`${profile.profile_id}-${index}`}>
      <h4>{profile.profile_id}</h4>
      <p>Route: {profile.route}</p>
      <DetailList title="Missing information / unknowns" items={profile.missing_information} />
      <DetailList title="Evidence" items={profile.evidence} />
    </article>)}
    <DetailList title="Limitations" items={evaluation.limitations} />
  </section>;
}