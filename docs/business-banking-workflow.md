# Business banking workflow alignment

The user-provided **Business Banking – Automated Credit Decision Recommendation (Existing Process)** diagram is a workflow requirement, not verified lender authorization. The application must not claim complete alignment until the gaps below are resolved and tested.

## Required sequence

1. Receive source extracts cleansed of PII by the source owner. Do not claim that upload or CSV formula sanitization anonymizes data.
2. Map Salesforce objects and join only explicit Account, Loan, CreditReport, response, entity and analysis identifiers. Retain all source attributes and provenance. Missing mappings remain UNKNOWN and allow diagnostic review, never guessed relationships.
3. Establish eligibility before interpreting credit-strategy candidates: Quick App, group credit at/below USD 250,000, and **not real-estate secured**, as stated in this diagram. Unknown eligibility remains review. Real-estate security is not synonymous with investment-real-estate purpose or a generic secured flag.
4. Build one feature set and one output row per explicit loan, not per source observation. Evaluate the existing knockout criteria and remaining supplied strategy only; no invented business rules.
5. Return loan identifier, non-operational recommendation candidate, rationale and evidence-backed diagnostic reasons. Production final action remains MANUAL_REVIEW; TEST remains simulation/no operational action. Approved lender adverse-action mappings are not available and must not be fabricated.
6. Compare against explicitly matched historical human decisions, showing match rate, confusion matrix and discrepancies when a validated historical dataset and label contract exist. Local file validation is **not** this outcome validation.
7. Underwriters review recommendations and rationale and make the actual decision outside the current application. Nothing is auto-decided. Review notes are not final decisions, policy approval or verified evidence.
8. Policy changes require a versioned, reviewed configuration and regression testing. Neither chat nor commentary may modify engine outcomes or clear conflicts.

## Alignment and unresolved items

Each explicit loan now includes `normalized_data.business_banking_workflow`: ordered eligibility, data preparation, knockouts, remaining strategy, output, historical-validation and underwriter-review stages. The UI renders these separately from the pinned-draft diagnostics, and Claude receives bounded eligibility/candidate/missing-input/conflict status. This additive trace does not replace authoritative v4 paths or authorize lending. Exact canonical Loan columns `group_credit_usd` and `real_estate_secured` may supply the diagram's distinct eligibility inputs; money must satisfy the existing exact USD/actual-dollar contract. Existing Salesforce fields are **not** newly assigned those semantics. Missing/invalid inputs yield UNKNOWN eligibility and REFER; out-of-scope eligibility or graph defects prevent interpreting a downstream candidate as eligible.

| Requirement | Current status |
| --- | --- |
| Explicit-ID joins and source-cell provenance | Implemented for supported canonical/flattened mappings; unmapped fields stay visible |
| Partial mapping continuation | Diagnostic review supported for a readable, valid three-workbook set; no fabricated loans |
| Knockout diagnostics | Public-record codes 1–3, explicit negative EBITDA, bankruptcy discharge timing, age, linked typed average personal FICO and SBSS supported when evidence is available |
| One row per loan | JSON/CSV outputs use explicit loan records, not source-row counts |
| Human review; no automatic decisions | Enforced; commentary cannot override deterministic findings |
| USD 250K eligibility | **Unresolved source discrepancy:** existing pinned v4 draft contains USD 250M Tier I diagnostics. These are not equivalent to this diagram's USD 250K group-credit filter; do not silently rescale or claim eligibility alignment |
| Not real-estate secured | An explicit, approved source-field mapping is still needed; investment purpose or generic security status cannot substitute |
| Four query files plus other extracts | Diagram requires four; current import contract supports Query 1/2/4 only. Query 3's schema, role and explicit relationships must be supplied before extending the allowlist |
| PII-cleansed inputs | Source-owner responsibility; automatic anonymization is not implemented or verified |
| Historical match rate / confusion matrix | Not implemented; requires historical human labels and exact loan matching. No accuracy claim is supported |
| Lender adverse-action reason mapping | Not verified; internal diagnostic codes must not be presented as approved notice codes |
| Underwriter final decision capture / integrations | Not implemented; no funding, nCino, compliance clearance or adverse-action integration is claimed |

The existing `REQUEST_THRESHOLD_100M_VS_100K` and `INCREASE_WAIT_6_VS_12_MONTHS` conflicts remain unresolved. This diagram reinforces a USD 100K condition but does not silently amend the other pinned sources. No threshold or lender-authority decision is inferred from file integrity alone.