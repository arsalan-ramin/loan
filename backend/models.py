"""Shared, validated API contracts."""

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, computed_field, field_validator

from .rule_catalog import guideline_rules
from .datasets import mtb_query_ids
from .underwriting_models import Mode, UnderwritingBatch, SourceEvidence, UnderwritingRuleResult


def uid() -> str:
    return uuid4().hex


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Rule(BaseModel):
    id: str = Field(default_factory=uid, min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["required_text", "forbidden_text", "min_words", "required_metadata"]
    value: str = Field(min_length=1, max_length=500)
    severity: Literal["review", "fail"] = "review"
    enabled: bool = True

    @field_validator("value")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Rule values must not be blank.")
        return value.strip()


class LoanInspection(BaseModel):
    report_id: str
    loan_id: str
    topic: Literal["blockers", "rules", "evidence"]
    recommendation_path: str
    final_action: str
    missing_inputs: list[str]
    conflicts: list[str]
    rules: list[UnderwritingRuleResult]
    evidence: list[SourceEvidence]
    total_rules: int
    total_evidence: int


class AccountCandidate(BaseModel):
    account_id: str
    name: str | None = None
    loan_count: int = 0
    loan_ids: list[str] = Field(default_factory=list)


class AccountCandidatePage(BaseModel):
    results: list[AccountCandidate]
    total: int
    offset: int
    limit: int
    account_count: int = 0
    refreshed_preflight: "PreflightResult | None" = None


class Message(BaseModel):
    id: str = Field(default_factory=uid)
    role: Literal["assistant", "user"]
    content: str
    created_at: str = Field(default_factory=now)
    origin: Literal["conversation", "local_assistant"] = "conversation"
    inspection: LoanInspection | None = None


class Document(BaseModel):
    id: str = Field(default_factory=uid)
    name: str
    size: int
    extension: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    content: str = ""


class Step(BaseModel):
    agent: str
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    detail: str = "Waiting for the previous agent."


class Finding(BaseModel):
    rule_id: str
    rule_name: str
    status: Literal["pass", "review", "fail"]
    detail: str
    document: str
    evidence: str


class PolicyRuleResult(BaseModel):
    rule_id: str
    status: Literal["triggered", "not_triggered", "unknown"]
    route: str | None = None
    page: int | None
    detail: str
    evidence: list[str] = Field(default_factory=list)


class PolicyProfileResult(BaseModel):
    profile_id: str
    route: str
    missing_information: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class PolicyEvaluation(BaseModel):
    policy_version: str
    source_document: str
    source_sha256: str | None = None
    policy_status: Literal["draft"] = "draft"
    recommended_route: str
    decision_recommendation: None = None
    requires_human_approval: Literal[True] = True
    missing_information: list[str] = Field(default_factory=list)
    rule_results: list[PolicyRuleResult] = Field(default_factory=list)
    profiles: list[PolicyProfileResult] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class PolicyExplanation(BaseModel):
    model_config = {"extra": "forbid"}
    recommended_route: str
    decision_recommendation: None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    positive_factors: list[str]
    risk_factors: list[str]
    triggered_rules: list[str]
    missing_information: list[str]
    explanation: str = Field(min_length=1)
    requires_human_approval: Literal[True]


class UnderwritingExplanation(PolicyExplanation):
    """Aggregate commentary only; per-loan explanations stay deterministic."""

    loan_ids: list[str]
    evidence_refs: list[str]


class CheckReconciliation(BaseModel):
    model_config = {"extra": "forbid"}
    rule_id: str = Field(min_length=1, max_length=128)
    evidence_state: Literal["MAPPED_EVIDENCE_BLOCKED", "MISSING_OR_UNVERIFIED_INPUT", "CONFLICTING_EVIDENCE", "POLICY_UNRESOLVED"]
    rationale: str = Field(min_length=1, max_length=600)
    next_step: str = Field(min_length=1, max_length=400)
    evidence_refs: list[str] = Field(max_length=3)


class LoanReviewOutput(UnderwritingExplanation):
    """Separate AI commentary, never an operational recommendation."""

    recommended_route: Literal["MANUAL_REVIEW"]
    confidence: None = None
    positive_factors: list[str] = Field(max_length=3)
    risk_factors: list[str] = Field(max_length=3)
    missing_information: list[str] = Field(max_length=3)
    explanation: str = Field(min_length=1, max_length=4000)
    loan_ids: list[str] = Field(min_length=1, max_length=1)
    triggered_rules: list[str] = Field(max_length=3)
    evidence_refs: list[str] = Field(max_length=3)
    check_reconciliation: list[CheckReconciliation] = Field(default_factory=list, max_length=40)


class LoanAIReview(BaseModel):
    id: str = Field(default_factory=uid)
    created_at: str = Field(default_factory=now)
    report_id: str
    loan_id: str
    model: str
    analysis: LoanReviewOutput
    context_note: str


class Report(BaseModel):
    id: str = Field(default_factory=uid)
    created_at: str = Field(default_factory=now)
    decision: Literal["approved", "needs_review", "rejected"]
    summary: str
    findings: list[Finding]
    document_count: int
    llm_analysis: str | None = None
    llm_model: str | None = None
    llm_error: str | None = None
    policy_evaluation: PolicyEvaluation | None = None
    llm_policy_output: PolicyExplanation | None = None
    underwriting: UnderwritingBatch | None = None


def default_rules() -> list[Rule]:
    return [
        Rule(id="readable-content", name="Meaningful document content", kind="min_words", value="30"),
        Rule(id="draft-check", name="No draft watermark", kind="forbidden_text", value="DRAFT", severity="review"),
    ]


AGENTS = ["Intake agent", "Extraction agent", "Rules agent", "Decision agent", "Memory agent"]


class PreflightResult(BaseModel):
    created_at: str = Field(default_factory=now)
    input_fingerprint: str
    recognized_mtb: bool
    query_roles: list[int]
    missing_query_roles: list[int]
    duplicate_query_roles: list[int] = Field(default_factory=list)
    query_file_set_valid: bool = False
    document_count: int
    attribute_count: int = 0
    attributes: list[str] = Field(default_factory=list)
    mapping_warnings: list[str] = Field(default_factory=list)
    unmapped_attribute_count: int = 0
    unmapped_attributes: list[str] = Field(default_factory=list)
    unavailable_inputs: list[str] = Field(default_factory=list)
    loan_count: int
    account_count: int = 0
    account_index_version: int = 0
    account_candidates: list[AccountCandidate] = Field(default_factory=list, exclude=True)
    issue_count: int
    priority_issues: list[str]
    suggested_next_step: str
    comparison: str
    # This is validation, not a saved underwriting decision/report.
    external_processing: Literal[False] = False


class ReviewNote(BaseModel):
    id: str = Field(default_factory=uid)
    created_at: str = Field(default_factory=now)
    report_id: str
    loan_id: str
    text: str
    kind: Literal["note", "acknowledgment"]
    provenance: Literal["user_assertion_not_verified_evidence"] = "user_assertion_not_verified_evidence"


class Session(BaseModel):
    id: str = Field(default_factory=uid)
    title: str = "Untitled review"
    mode: Mode = "TEST"
    selected_account_id: str | None = None
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    status: Literal["awaiting_upload", "ready", "processing", "completed", "failed"] = "awaiting_upload"
    messages: list[Message] = Field(default_factory=list)
    documents: list[Document] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=default_rules)
    steps: list[Step] = Field(default_factory=lambda: [Step(agent=name) for name in AGENTS])
    report: Report | None = None
    error: str | None = None
    preflight: PreflightResult | None = None
    previous_preflight: PreflightResult | None = None
    review_notes: list[ReviewNote] = Field(default_factory=list)
    loan_ai_reviews: list[LoanAIReview] = Field(default_factory=list)

    @computed_field
    @property
    def policy_rules(self) -> list[dict]:
        """Current policy applicability is separate from optional user document checks."""
        return guideline_rules() if any(mtb_query_ids(doc.name, doc.metadata) for doc in self.documents) else []


class CreateSession(BaseModel):
    model_config = {"extra": "forbid"}
    title: str = Field(default="Untitled review", min_length=1, max_length=120)
    mode: Mode = "TEST"


class ModeRequest(BaseModel):
    model_config = {"extra": "forbid"}
    mode: Mode


class AccountSelectionRequest(BaseModel):
    model_config = {"extra": "forbid"}
    account_id: str = Field(min_length=1, max_length=128)


class ChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    loan_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("content")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Message must not be blank.")
        return value.strip()


class RulesRequest(BaseModel):
    rules: list[Rule] = Field(max_length=50)

    @field_validator("rules")
    @classmethod
    def validate_rules(cls, rules: list[Rule]) -> list[Rule]:
        if len({rule.id for rule in rules}) != len(rules):
            raise ValueError("Rule IDs must be unique.")
        for rule in rules:
            if rule.kind == "min_words" and (not rule.value.isascii() or not rule.value.isdecimal() or not 1 <= int(rule.value) <= 1_000_000):
                raise ValueError("Minimum word counts must be integers between 1 and 1,000,000.")
        return rules