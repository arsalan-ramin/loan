export interface Rule {
  id: string;
  name: string;
  kind: 'required_text' | 'forbidden_text' | 'min_words' | 'required_metadata';
  value: string;
  severity: 'review' | 'fail';
  enabled: boolean;
}

export interface PolicyRuleDefinition {
  id: string;
  name: string;
  category: string;
  page: number;
  description: string;
  state: 'enforced' | 'conditional' | 'blocked';
  reason: string;
}

export interface ExcelSheet {
  name: string;
  headers: string[];
  header_row: number | null;
  data_rows: number;
  columns: number;
  is_reference: boolean;
}

export interface Document {
  id: string;
  name: string;
  size: number;
  extension: string;
  metadata: Record<string, unknown> & {
    sheets?: ExcelSheet[];
    data_row_count?: number;
    warning?: string;
  };
  content?: string;
}

export interface PolicyEvaluation {
  policy_version: string;
  source_document: string;
  source_sha256: string | null;
  policy_status: 'draft';
  recommended_route: string;
  decision_recommendation: null;
  requires_human_approval: true;
  missing_information: string[];
  rule_results: {
    rule_id: string;
    status: 'triggered' | 'not_triggered' | 'unknown';
    route: string | null;
    page: number | null;
    detail: string;
    evidence: string[];
  }[];
  profiles: {
    profile_id: string;
    route: string;
    missing_information: string[];
    evidence: string[];
  }[];
  limitations: string[];
}

export type Mode = 'TEST' | 'PRODUCTION';
export type RuleStatus = 'PASS' | 'FAIL' | 'UNKNOWN' | 'NOT_APPLICABLE';
export type RecommendationPath = 'DATA_QUALITY_HOLD' | 'POLICY_REVIEW_REQUIRED' | 'SIMULATED_APPROVE_PATH' | 'SIMULATED_DECLINE_PATH' | 'SIMULATED_REVIEW' | 'MANUAL_REVIEW';
export type EligibilityRoute = 'QUICK_APP' | 'FULL_UNDERWRITING' | 'PRODUCT_REVIEW' | 'DATA_REVIEW' | 'POLICY_REVIEW';
export type CreditAssessment = 'SIMULATED_APPROVAL_CANDIDATE' | 'SIMULATED_DECLINE_CANDIDATE' | 'MANUAL_REVIEW' | 'POLICY_REVIEW' | 'DATA_REVIEW' | 'COMPLIANCE_REVIEW';

export interface SourceEvidence {
  id: string;
  workbook: string;
  sheet: string;
  row: number;
  record_id: string | null;
  field: string;
  value: string | boolean | null;
}

export interface UnderwritingRuleResult {
  rule_id: string;
  status: RuleStatus;
  detail: string;
  evidence_refs: string[];
  page: number | null;
  policy_reference: string | null;
}

export interface LoanRecommendation {
  loan_id: string;
  borrower_account_id: string | null;
  decision_status: 'Human Review';
  currency: 'USD';
  // Decimal money is serialized as exact strings by the backend. Never coerce to Number.
  normalized_amounts_usd: Record<string, string | null>;
  normalized_data: Record<string, unknown>;
  mode: Mode;
  policy_version: string;
  rule_results: UnderwritingRuleResult[];
  source_evidence: SourceEvidence[];
  missing_inputs: string[];
  conflicts: string[];
  recommendation_path: RecommendationPath;
  eligibility_route?: EligibilityRoute | null;
  credit_assessment?: CreditAssessment | null;
  authorization?: 'NOT_ISSUED';
  strategy_branch?: string | null;
  expanded_spec?: ExpandedSpecEvaluation | null;
  final_action: 'NO_OPERATIONAL_ACTION' | 'MANUAL_REVIEW';
  human_review_required: true;
  explanation: string;
  llm_error: string | null;
}

export interface ExpandedSpecEvaluation {
  version: string;
  source_document: string;
  source_sha256: string;
  source_available: boolean;
  eligibility_route: EligibilityRoute;
  simulated_credit_route: string;
  authorized_decision: 'NOT_ISSUED';
  candidate_reason_codes: string[];
  missing_inputs: string[];
  policy_conflicts: string[];
  input_fingerprint_sha256: string;
  prompt_version: string;
  reviewer_id: null;
  decision_history: string[];
  human_review_required: true;
  rule_results: {
    rule_id: string; scope: string; precedence: number; version: string;
    source_document: string; page: number; predicate: string;
    required_inputs: string[]; missing_data_action: string; exception_path: string;
    decline_reason_codes: string[]; status: 'PASS' | 'FAIL' | 'REVIEW' | 'NOT_EVALUATED';
    detail: string; evidence_refs: string[];
  }[];
  decline_reasons: {
    code: string; label: string; page: number; source_document: string;
    status: 'PASS' | 'FAIL' | 'REVIEW' | 'NOT_EVALUATED'; disposition: string;
    detail: string; required_inputs: string[]; evidence_refs: string[];
    adverse_action_code: null;
  }[];
}

export interface UnderwritingBatch {
  mode: Mode;
  currency: 'USD';
  policy_version: string;
  source_document: 'LLM_Loan_Approval_Design_Guide_v4_USD_Copilot.pdf';
  source_available: boolean;
  implementation_guide_source_document?: 'MTB_LLM_Underwriting_Complete_Implementation_Guide.pdf';
  implementation_guide_source_sha256?: string;
  implementation_guide_available?: boolean;
  expanded_spec_source_document?: string;
  expanded_spec_source_sha256?: string;
  expanded_spec_available?: boolean;
  data_quality_issues: string[];
  mapping_warnings?: string[];
  unmapped_source_fields?: string[];
  unavailable_inputs?: string[];
  loan_results: LoanRecommendation[];
}

export interface LoanInspection {
  report_id: string;
  loan_id: string;
  topic: 'blockers' | 'rules' | 'evidence';
  recommendation_path: string;
  final_action: string;
  missing_inputs: string[];
  conflicts: string[];
  rules: UnderwritingRuleResult[];
  evidence: SourceEvidence[];
  total_rules: number;
  total_evidence: number;
}

export interface PreflightResult {
  created_at: string;
  input_fingerprint: string;
  recognized_mtb: boolean;
  query_roles: number[];
  missing_query_roles: number[];
  duplicate_query_roles: number[];
  query_file_set_valid: boolean;
  document_count: number;
  attribute_count: number;
  attributes: string[];
  unmapped_attribute_count?: number;
  unmapped_attributes?: string[];
  mapping_warnings?: string[];
  unavailable_inputs?: string[];
  loan_count: number;
  account_count: number;
  account_index_version?: number;
  issue_count: number;
  priority_issues: string[];
  suggested_next_step: string;
  comparison: string;
  external_processing: false;
}

export interface AccountCandidate {
  account_id: string;
  name: string | null;
  loan_count: number;
  loan_ids: string[];
}

export interface AccountCandidatePage {
  results: AccountCandidate[];
  total: number;
  offset: number;
  limit: number;
  account_count: number;
  refreshed_preflight?: PreflightResult | null;
}

export interface ReviewNote {
  id: string;
  created_at: string;
  report_id: string;
  loan_id: string;
  text: string;
  kind: 'note' | 'acknowledgment';
  provenance: 'user_assertion_not_verified_evidence';
}

export interface LoanAIReview {
  id: string;
  created_at: string;
  report_id: string;
  loan_id: string;
  model: string;
  context_note: string;
  analysis: {
    recommended_route: 'MANUAL_REVIEW';
    decision_recommendation: null;
    confidence: null;
    requires_human_approval: true;
    loan_ids: string[];
    explanation: string;
    positive_factors: string[];
    risk_factors: string[];
    missing_information: string[];
    triggered_rules: string[];
    evidence_refs: string[];
    check_reconciliation?: Array<{
      rule_id: string;
      evidence_state: 'MAPPED_EVIDENCE_BLOCKED' | 'MISSING_OR_UNVERIFIED_INPUT' | 'CONFLICTING_EVIDENCE' | 'POLICY_UNRESOLVED';
      rationale: string;
      next_step: string;
      evidence_refs: string[];
    }>;
  };
}

export interface Session {
  id: string;
  title: string;
  mode?: Mode;
  selected_account_id?: string | null;
  created_at: string;
  updated_at: string;
  status: 'awaiting_upload' | 'ready' | 'processing' | 'completed' | 'failed';
  messages: { id: string; role: 'assistant' | 'user'; content: string; created_at: string; origin?: 'conversation' | 'local_assistant'; inspection?: LoanInspection | null }[];
  preflight?: PreflightResult | null;
  review_notes?: ReviewNote[];
  loan_ai_reviews?: LoanAIReview[];
  documents: Document[];
  rules: Rule[];
  policy_rules?: PolicyRuleDefinition[];
  steps: { agent: string; status: 'pending' | 'running' | 'completed' | 'failed'; detail: string }[];
  report: null | {
    id: string;
    created_at: string;
    decision: 'approved' | 'needs_review' | 'rejected';
    summary: string;
    llm_analysis?: string | null;
    llm_model?: string | null;
    llm_error?: string | null;
    policy_evaluation?: PolicyEvaluation | null;
    underwriting?: UnderwritingBatch | null;
    findings: {
      rule_id: string;
      rule_name: string;
      status: 'pass' | 'review' | 'fail';
      detail: string;
      document: string;
      evidence: string;
    }[];
    document_count: number;
  };
  error: null | string;
}