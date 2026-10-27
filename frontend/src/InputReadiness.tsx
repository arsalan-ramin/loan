import type { UnderwritingRuleResult } from './types';

const requirementLabels: Record<string, string> = {
  explicit_iso_as_of_date: 'Loan as-of date: provide an explicit ISO calendar date.',
  report_selection_requires_explicit_as_of_date: 'Credit-report selection is blocked by the missing loan as-of date—not by missing bureau columns.',
  completed_report_date_missing_or_ambiguous: 'Credit-report date: supply a valid date for each completed report.',
  borrower_personal_fico_for_screenshot_average: 'A selected, explicitly typed personal report is needed for the borrower FICO average.',
  sbss_response_id_not_exported: 'SBSS response record ID was not exported. A separately loan-linked SBSS score may still be available.',
};

export default function InputReadiness({ items, rules }: { items: string[]; rules: UnderwritingRuleResult[] }) {
  const byId = new Map(rules.map((rule) => [rule.rule_id, rule]));
  // Legacy snapshots may contain more rule IDs than their bounded rule list.
  const isRule = (item: string) => byId.has(item) || /^[A-Z][A-Z0-9_]+$/.test(item);
  const requirements = items.filter((item) => !isRule(item));
  const checks = items.filter(isRule);
  return <section aria-label="Input and check readiness">
    <h5>Missing data or selection requirements</h5>
    {requirements.length ? <ul>{requirements.map((item) => <li key={item}>{requirementLabels[item] ?? item}</li>)}</ul>
      : <p>No additional data requirements listed.</p>}
    <h5>Unresolved checks—not missing Excel columns</h5>
    <p>A mapped column can be present while its report is not selectable, its value conflicts, or another required input is unavailable. UNKNOWN does not mean passed.</p>
    {checks.length ? checks.map((id) => {
      const rule = byId.get(id);
      return <div className="policy-result" key={id}>
        <h6>{id}</h6>
        <p>{rule?.detail ?? 'See the full report rule trace for the reason this check is unresolved.'}</p>
        {rule && <p>{rule.evidence_refs.length
          ? `${rule.evidence_refs.length} mapped source evidence references retained. See the rule trace for the associated cells.`
          : 'No applicable source-cell references available for this check.'}</p>}
      </div>;
    }) : <p>No unresolved checks listed.</p>}
  </section>;
}