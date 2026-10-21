"""Standalone, escaped HTML reports with no external assets or scripts."""

import json
from html import escape

from .models import Session
from .underwriting_models import SOURCE_SHA256


def render_report(session: Session) -> str:
    report = session.report
    if report is None:
        raise ValueError("No report is available.")
    rows = "".join(
        f'<tr><td>{escape(f.document)}</td><td>{escape(f.rule_name)}</td>'
        f'<td><span class="{f.status}">{escape(f.status.upper())}</span></td>'
        f'<td>{escape(f.detail)}<pre>{escape(f.evidence)}</pre></td></tr>'
        for f in report.findings
    )
    documents = "".join(
        f'<article><h3>{escape(doc.name)}</h3><dl>' +
        "".join(f'<dt>{escape(str(key))}</dt><dd>{escape(str(value))}</dd>' for key, value in doc.metadata.items()) +
        '</dl></article>' for doc in session.documents
    )
    rules = "".join(f'<li>{escape(r.name)} — {escape(r.kind)}: {escape(r.value)} '
                    f'({escape(r.severity)}; {"enabled" if r.enabled else "disabled"})</li>' for r in session.rules)
    analysis = (
        f'<section><h2>Claude explanation</h2><p>Model: {escape(report.llm_model or "Anthropic")}. '
        f'AI-generated commentary; rule findings remain authoritative.</p><pre>{escape(report.llm_analysis)}</pre></section>'
        if report.llm_analysis else
        f'<section><h2>Claude explanation unavailable</h2><p>{escape(report.llm_error)}</p></section>'
        if report.llm_error else ""
    )
    policy_html = ""
    if report.policy_evaluation:
        policy = report.policy_evaluation
        checks = "".join(f'<li><strong>{escape(r.rule_id)}: {escape(r.status)}</strong> — '
                 f'{"PDF p." + str(r.page) if r.page else "Data diagnostic (no PDF citation)"}. '
                         f'{escape(r.detail)}<pre>{escape(chr(10).join(r.evidence))}</pre></li>' for r in policy.rule_results)
        missing = "".join(f'<li>{escape(item)}</li>' for item in policy.missing_information)
        limits = "".join(f'<li>{escape(item)}</li>' for item in policy.limitations)
        policy_html = (f'<section><h2>{"V4 compatibility summary" if report.underwriting is not None else "Draft underwriting route"}: {escape(policy.recommended_route)}</h2>'
                       '<p><strong>Not a lending approval or denial. Authorized review required.</strong></p>'
                       f'<p>{escape(policy.source_document)} · {escape(policy.policy_version)} · SHA-256 {escape(policy.source_sha256 or "unavailable")}</p>'
                       f'<h3>Policy checks and page citations</h3><ul>{checks}</ul><h3>Missing information</h3><ul>{missing}</ul>'
                       f'<h3>Controls and limitations</h3><ul>{limits}</ul></section>')
    underwriting_html = ""
    if report.underwriting is not None:
        batch = report.underwriting

        def items(values: list[str]) -> str:
            return "".join(f"<li>{escape(value)}</li>" for value in values) or "<li>None reported.</li>"

        loans = []
        for loan in batch.loan_results:
            amounts = "".join(f'<dt>{escape(key)}</dt><dd>USD {format(value, "f") if value is not None else "unknown"}</dd>'
                              for key, value in loan.normalized_amounts_usd.items())
            checks = "".join(
                f'<tr><td>{escape(rule.rule_id)}</td><td>{escape(rule.status)}</td>'
                f'<td>{escape(rule.policy_reference or "Data diagnostic — no PDF citation")}</td>'
                f'<td>{escape(rule.detail)}<pre>{escape(chr(10).join(rule.evidence_refs))}</pre></td></tr>'
                for rule in loan.rule_results)
            evidence = "".join(
                f'<li>{escape(item.id)} — {escape(item.workbook)} / {escape(item.sheet)} / row {item.row} / '
                f'{escape(item.field)}<pre>{escape(str(item.value))}</pre></li>' for item in loan.source_evidence)
            diagnostics = escape(json.dumps(loan.normalized_data, ensure_ascii=False, indent=2))
            expanded_html = ""
            if loan.expanded_spec is not None:
                spec = loan.expanded_spec
                reason_rows = "".join(
                    f'<tr><td>{escape(item.code)} — {escape(item.label)}</td><td>{item.status}</td>'
                    f'<td>{item.disposition}</td><td>{escape(item.detail)}<pre>{escape(chr(10).join(item.evidence_refs))}</pre></td></tr>'
                    for item in spec.decline_reasons)
                expanded_html = (
                    '<h4>Expanded specification · draft simulation diagnostics</h4>'
                    f'<p>{escape(spec.source_document)} · Verified: {spec.source_available} · SHA-256 {spec.source_sha256}</p>'
                    f'<p>Eligibility: {spec.eligibility_route} · Simulated credit route: {spec.simulated_credit_route} · '
                    f'Authorized decision: {spec.authorized_decision}</p>'
                    '<p>Internal reason codes are not approved adverse-action codes. No denial or notice issued.</p>'
                    f'<h5>Candidate reason codes</h5><ul>{items(spec.candidate_reason_codes)}</ul>'
                    f'<h5>Expanded missing inputs</h5><ul>{items(spec.missing_inputs)}</ul>'
                    f'<h5>Expanded policy conflicts</h5><ul>{items(spec.policy_conflicts)}</ul>'
                    '<details><summary>Complete decline-reason inventory</summary><table><thead><tr>'
                    '<th>Internal reason</th><th>Status</th><th>Disposition</th><th>Detail / evidence</th>'
                    f'</tr></thead><tbody>{reason_rows}</tbody></table></details>'
                    '<details><summary>Expanded rule registry and audit</summary>'
                    f'<pre>{escape(spec.model_dump_json(indent=2))}</pre></details>')
            loans.append(f'<article><h3>Loan {escape(loan.loan_id)}</h3>'
                         f'<p>Borrower account ID: {escape(loan.borrower_account_id or "Not provided")}</p>'
                         f'<p>Path: {escape(loan.recommendation_path)} · Final action: {escape(loan.final_action)} · Human review required.</p>'
                         '<h4>Separate non-operational outputs</h4><dl>'
                         f'<dt>Eligibility route</dt><dd>{escape(loan.eligibility_route or "Not reported")}</dd>'
                         f'<dt>Credit assessment candidate</dt><dd>{escape(loan.credit_assessment or "Not reported")} — candidate/review only</dd>'
                         f'<dt>Authorization</dt><dd>{escape(loan.authorization)}</dd>'
                         f'<dt>Strategy branch (diagnostic)</dt><dd>{escape(loan.strategy_branch or "Not reported")}</dd></dl>'
                         '<details><summary>Implementation guide diagnostics · stages, audit and normalized features</summary>'
                         '<p>Guide diagnostics and candidate features are not credited rule outcomes or operational decisions. '
                         'Out-of-scope checks remain NOT_EVALUATED.</p>'
                         f'<pre>{diagnostics}</pre></details>'
                         f'<h4>Exact USD amounts</h4><dl>{amounts}</dl>'
                         f'{expanded_html}'
                         f'<h4>Missing inputs</h4><ul>{items(loan.missing_inputs)}</ul>'
                         f'<h4>Conflicts</h4><ul>{items(loan.conflicts)}</ul>'
                         f'<h4>Rule statuses and citations</h4><table><thead><tr><th>Rule</th><th>Status</th><th>Source</th><th>Evidence references</th></tr></thead><tbody>{checks}</tbody></table>'
                         f'<h4>Deterministic per-loan explanation (not LLM-generated)</h4><p>{escape(loan.explanation)}</p>'
                         f'<h4>Source evidence</h4><ul>{evidence}</ul></article>')
        underwriting_html = (f'<section><h2>V4 underwriting · {escape(batch.mode)}</h2>'
                             '<p><strong>No lending approval or denial. Authorized human review required.</strong></p>'
                             f'<p>{escape(batch.policy_version)} · {escape(batch.source_document)} · '
                             f'Source verified: {batch.source_available} · SHA-256 {SOURCE_SHA256 if batch.source_available else "unavailable"}</p>'
                             f'<p>Implementation guide: {escape(batch.implementation_guide_source_document)} · '
                             f'Source verified: {batch.implementation_guide_available} · '
                             f'Pinned SHA-256 {escape(batch.implementation_guide_source_sha256)}</p>'
                             '<p>Exact actual USD dollars. Verified draft evidence does not confer lending authority. '
                             'Claude commentary is aggregate only; every per-loan explanation remains deterministic.</p>'
                             f'<h3>Batch data quality</h3><ul>{items(batch.data_quality_issues)}</ul>'
                             f'{"".join(loans) or "<p>No explicit loan records. No loans were fabricated from source rows or empty templates.</p>"}</section>')
    decision_label = "DOCUMENT CHECKS — HUMAN REVIEW REQUIRED" if report.policy_evaluation or report.underwriting is not None else report.decision.replace('_', ' ').upper()
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>Agent · {escape(session.title)}</title>
<style>
body{{font:15px/1.6 system-ui,sans-serif;background:#f5f5f0;color:#202c29;margin:0;padding:48px 24px}}
main{{max-width:1100px;margin:auto}}header{{border-bottom:2px solid #244e40;padding-bottom:24px}}
.brand{{letter-spacing:4px;color:#416d57;font-weight:800}}h1{{font-size:36px;margin:16px 0 8px}}
.badge{{display:inline-block;padding:8px 16px;border:1px solid #b8c9bf;border-radius:30px;background:#e5eee7;font-weight:700}}
section,article{{background:white;padding:24px;margin-top:24px;border:1px solid #e0e4df;border-radius:12px}}
table{{width:100%;border-collapse:collapse}}th,td{{padding:14px;text-align:left;border-bottom:1px solid #e5e7e2;vertical-align:top}}
th{{font-size:12px;text-transform:uppercase;color:#65756b}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.6 monospace;background:#f5f6f2;padding:12px}}
.pass{{color:#247044}}.review{{color:#92640b}}.fail{{color:#b23a35}}dl{{display:grid;grid-template-columns:180px 1fr;gap:8px}}dd{{margin:0;overflow-wrap:anywhere}}dt{{color:#65756b}}footer{{color:#65756b;margin-top:24px}}
@media print{{body{{padding:0;background:white}}section,article{{break-inside:avoid}}}}
@media(max-width:600px){{body{{padding:16px}}section{{padding:12px;overflow:auto}}dl{{grid-template-columns:1fr}}}}
</style></head><body><main><header><div class="brand">AGENT / DOCUMENT INTELLIGENCE</div>
<h1>{escape(session.title)}</h1><p>Report {escape(report.id)} · {escape(report.created_at)}</p>
<span class="badge">{escape(decision_label)}</span><p>{escape(report.summary)}</p></header>
{underwriting_html}
{policy_html if report.underwriting is None else ''}
<section><h2>Decision evidence</h2><table><thead><tr><th>Document</th><th>Rule</th><th>Result</th><th>Evidence</th></tr></thead><tbody>{rows}</tbody></table></section>
<section><h2>Optional document checks</h2><ul>{rules or '<li>No optional text checks configured.</li>'}</ul><p>These are separate from underwriting policy rules. Text checks use case-insensitive literal substring matching. Each enabled check applies to each document.</p></section>
{analysis}
<h2>Extracted metadata</h2>{documents}
<footer>Generated locally by Agent. Decisions are deterministic evaluations of configured rules, not professional advice. No OCR or signature verification is performed. Use your browser’s Print → Save as PDF for a PDF copy.</footer>
</main></body></html>'''