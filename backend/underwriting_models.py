"""Parent integration contract for deterministic, non-operational underwriting.

Import ``UnderwritingBatch`` here and ``evaluate_underwriting`` from the engine.
JSON serialization uses decimal strings, never binary floating-point money.
These models intentionally cannot represent an operational approval or denial.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Mode = Literal["TEST", "PRODUCTION"]
RuleStatus = Literal["PASS", "FAIL", "UNKNOWN", "NOT_APPLICABLE"]
RecommendationPath = Literal[
    "DATA_QUALITY_HOLD", "POLICY_REVIEW_REQUIRED", "SIMULATED_APPROVE_PATH",
    "SIMULATED_DECLINE_PATH", "SIMULATED_REVIEW", "MANUAL_REVIEW",
]
EligibilityRoute = Literal["QUICK_APP", "FULL_UNDERWRITING", "PRODUCT_REVIEW", "DATA_REVIEW", "POLICY_REVIEW"]
CreditAssessment = Literal[
    "SIMULATED_APPROVAL_CANDIDATE", "SIMULATED_DECLINE_CANDIDATE", "MANUAL_REVIEW",
    "POLICY_REVIEW", "DATA_REVIEW", "COMPLIANCE_REVIEW",
]
Authorization = Literal["NOT_ISSUED"]

USD_25K = Decimal("25000")
USD_100K = Decimal("100000.00")
USD_250K = Decimal("250000")
USD_25M = Decimal("25000000.00")
USD_100M = Decimal("100000000.00")
USD_250M = Decimal("250000000.00")
SOURCE_DOCUMENT = "LLM_Loan_Approval_Design_Guide_v4_USD_Copilot.pdf"
SOURCE_SHA256 = "9ed35356fdf8d7f638b6eb77c463df71f9a6dd17234a5fe31be251fda8ac18e7"
IMPLEMENTATION_GUIDE_DOCUMENT = "MTB_LLM_Underwriting_Complete_Implementation_Guide.pdf"
IMPLEMENTATION_GUIDE_SHA256 = "8a409bf6499129be8efe109d1a15f21ecfd95929b0674b993ab6d13c9c8e9467"
EXPANDED_SPEC_DOCUMENT = "MTB_Underwriting_Expanded_Technical_Specification.pdf"
EXPANDED_SPEC_SHA256 = "a4622c26c642f9546f0df9fd1634ed735d25be36e8042451ba702ddd958a326b"

# Internal legacy input aliases remain accepted; the public money contract is
# exclusively explicit USD names. No alias implies approved source semantics.
AMOUNT_NAMES = {
    "requested_amount": "loan_requested_amount_usd", "new_money": "new_money_usd",
    "tier_i": "tier1_group_credit_usd", "tier_ii": "tier2_group_credit_usd",
    "effective_tgc": "total_group_credit_usd", "ebitda": "ebitda_usd",
    "past_due": "total_past_due_usd", "credit_limit": "credit_limit_usd",
    "estimated_annual_revenue": "estimated_annual_revenue_usd",
    "business_net_income": "business_net_income_usd", "cash_and_equiv": "cash_and_equiv_usd",
    "net_worth": "net_worth_usd", "total_assets": "total_assets_usd",
    "total_liabilities": "total_liabilities_usd", "available_credit": "available_credit_usd",
    "bankruptcy_amount": "bankruptcy_amount_usd",
    "current_assets": "current_assets_usd", "current_liabilities": "current_liabilities_usd",
    "prior_revenue": "prior_revenue_usd", "current_revenue": "current_revenue_usd",
    "prior_expenses": "prior_expenses_usd", "current_expenses": "current_expenses_usd",
    "prior_profit": "prior_profit_usd", "current_profit": "current_profit_usd",
}


def exact_decimal(value: Any) -> Decimal:
    """Accept finite Decimal, integer, or plain decimal lexical text; reject floats.

    No currency symbols, grouping, exponent scaling, formulas, booleans or locale
    conversions. Negative values are valid for EBITDA, not exposure amounts.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("An exact decimal string, Decimal or integer is required")
    if isinstance(value, str) and not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", value.strip()):
        raise ValueError("Plain decimal lexical text is required")
    try:
        number = Decimal(value.strip() if isinstance(value, str) else value)
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid decimal") from None
    if not number.is_finite():
        raise ValueError("Finite decimals are required")
    return number


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class SourceEvidence(ContractModel):
    id: str
    workbook: str
    sheet: str
    row: int = Field(ge=1)
    record_id: str | None = None
    field: str
    # All numeric source values are lexical strings; invalid floats are never
    # echoed as monetary numbers. Complex objects are not reproduced.
    value: str | bool | None = None

    @field_validator("value", mode="before")
    @classmethod
    def safe_value(cls, value: Any) -> str | bool | None:
        if isinstance(value, float):
            raise ValueError("Float evidence is not permitted")
        if value is None or isinstance(value, (str, bool)):
            return value
        if isinstance(value, (int, Decimal)):
            return str(value)
        raise ValueError("Evidence must be scalar lexical data")


class UnderwritingRuleResult(ContractModel):
    rule_id: str
    status: RuleStatus
    detail: str
    evidence_refs: list[str] = Field(default_factory=list)
    page: int | None = Field(default=None, ge=1)
    policy_reference: str | None = None


class ExpandedRuleResult(ContractModel):
    rule_id: str
    scope: str
    precedence: int
    version: str = "expanded-draft-1"
    source_document: str = EXPANDED_SPEC_DOCUMENT
    page: int
    predicate: str
    required_inputs: list[str]
    missing_data_action: str
    exception_path: str
    decline_reason_codes: list[str] = Field(default_factory=list)
    status: Literal["PASS", "FAIL", "REVIEW", "NOT_EVALUATED"]
    detail: str
    evidence_refs: list[str] = Field(default_factory=list)


class DeclineReasonDiagnostic(ContractModel):
    code: str
    label: str
    page: int
    source_document: str = EXPANDED_SPEC_DOCUMENT
    status: Literal["PASS", "FAIL", "REVIEW", "NOT_EVALUATED"]
    disposition: Literal["DECLINE_CANDIDATE", "MANUAL_REVIEW", "DATA_REVIEW", "POLICY_REVIEW", "OUT_OF_SCOPE", "NOT_USED"]
    detail: str
    required_inputs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    adverse_action_code: None = None


class ExpandedSpecEvaluation(ContractModel):
    version: str = "expanded-draft-1"
    source_document: str = EXPANDED_SPEC_DOCUMENT
    source_sha256: str = EXPANDED_SPEC_SHA256
    source_available: bool
    eligibility_route: EligibilityRoute
    simulated_credit_route: Literal["SBA_APPROVAL_CANDIDATE", "CONVENTIONAL_APPROVAL_CANDIDATE", "SIMULATED_DECLINE_CANDIDATE", "FULL_UNDERWRITING", "DATA_REVIEW", "POLICY_REVIEW", "MANUAL_REVIEW", "COMPLIANCE_REVIEW"]
    authorized_decision: Authorization = "NOT_ISSUED"
    rule_results: list[ExpandedRuleResult]
    decline_reasons: list[DeclineReasonDiagnostic]
    candidate_reason_codes: list[str] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    policy_conflicts: list[str] = Field(default_factory=list)
    input_fingerprint_sha256: str
    prompt_version: str = "expanded-explanation-1"
    reviewer_id: None = None
    decision_history: list[str] = Field(default_factory=lambda: ["SIMULATION_ONLY; no authorized decision issued"])
    human_review_required: Literal[True] = True


class LoanRecommendation(ContractModel):
    loan_id: str
    borrower_account_id: str | None = None
    currency: Literal["USD"] = "USD"
    normalized_amounts_usd: dict[str, Decimal | None] = Field(default_factory=dict)
    # Non-money data/features are deliberately separate from normalized amounts.
    normalized_data: dict[str, Any] = Field(default_factory=dict)
    mode: Mode
    policy_version: str
    rule_results: list[UnderwritingRuleResult] = Field(default_factory=list)
    source_evidence: list[SourceEvidence] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    recommendation_path: RecommendationPath
    eligibility_route: EligibilityRoute | None = None
    credit_assessment: CreditAssessment | None = None
    authorization: Authorization = "NOT_ISSUED"
    strategy_branch: str | None = None
    expanded_spec: ExpandedSpecEvaluation | None = None
    final_action: Literal["NO_OPERATIONAL_ACTION", "MANUAL_REVIEW"] = "NO_OPERATIONAL_ACTION"
    human_review_required: Literal[True] = True
    explanation: str
    llm_error: str | None = None

    @field_validator("normalized_amounts_usd", mode="before")
    @classmethod
    def exact_money(cls, values: Any) -> dict[str, Decimal | None]:
        if not isinstance(values, dict):
            raise ValueError("Amounts must be a dictionary")
        if any(not isinstance(key, str) or not key.endswith("_usd") for key in values):
            raise ValueError("Monetary dictionary keys must end in _usd")
        return {key: None if value is None else exact_decimal(value) for key, value in values.items()}

    @model_validator(mode="after")
    def non_operational(self) -> LoanRecommendation:
        if self.mode == "PRODUCTION" and self.recommendation_path.startswith("SIMULATED_"):
            raise ValueError("Simulations are restricted to TEST mode")
        if self.mode == "TEST" and self.recommendation_path not in {
            "SIMULATED_APPROVE_PATH", "SIMULATED_DECLINE_PATH", "SIMULATED_REVIEW",
        }:
            raise ValueError("TEST recommendations must use the three simulation paths")
        if self.mode == "PRODUCTION" and self.final_action != "MANUAL_REVIEW":
            raise ValueError("Production requires manual review")
        if self.mode == "TEST" and self.final_action != "NO_OPERATIONAL_ACTION":
            raise ValueError("TEST cannot perform operational actions")
        return self


class UnderwritingBatch(ContractModel):
    mode: Mode
    currency: Literal["USD"] = "USD"
    policy_version: str
    source_document: Literal["LLM_Loan_Approval_Design_Guide_v4_USD_Copilot.pdf"] = SOURCE_DOCUMENT
    source_available: bool = False
    implementation_guide_source_document: Literal["MTB_LLM_Underwriting_Complete_Implementation_Guide.pdf"] = IMPLEMENTATION_GUIDE_DOCUMENT
    implementation_guide_source_sha256: str = IMPLEMENTATION_GUIDE_SHA256
    implementation_guide_available: bool = False
    expanded_spec_source_document: str = EXPANDED_SPEC_DOCUMENT
    expanded_spec_source_sha256: str = EXPANDED_SPEC_SHA256
    expanded_spec_available: bool = False
    data_quality_issues: list[str] = Field(default_factory=list)
    mapping_warnings: list[str] = Field(default_factory=list)
    unmapped_source_fields: list[str] = Field(default_factory=list)
    unavailable_inputs: list[str] = Field(default_factory=list)
    loan_results: list[LoanRecommendation] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_children(self) -> UnderwritingBatch:
        if any(loan.mode != self.mode or loan.policy_version != self.policy_version for loan in self.loan_results):
            raise ValueError("Batch and loan mode/policy version must agree")
        return self