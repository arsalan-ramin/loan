"""Local aggregate-FICO scenarios over saved TEST facts; never source corrections."""

import copy
import re
from decimal import Decimal

from .demo_workflow import FICO_MAX, FICO_MIN, bounded_demo_context, decide_demo_facts
from .models import Session


_INTENT = re.compile(r"\b(?:what[\s-]+if|suppose|assuming)\b", re.IGNORECASE)
_SCORE = r"(?:fico(?:\s+score)?|credit\s+score)(?:\s*/\s*(?:fico|credit\s+score))?"
_REQUEST = re.compile(
    rf"\s*(?:what[\s-]+if|suppose|assuming)\s+(?:the\s+)?(?:aggregate\s+|average\s+)?"
    rf"{_SCORE}(?:\s+(?:was|were|is|becomes|became)\s+|\s*[=:]\s*|\s+)"
    r"(?P<score>[+-]?\d+(?:\.\d+)?)\s*[?.!]*\s*", re.IGNORECASE,
)


def is_fico_what_if(query: str) -> bool:
    return bool(_INTENT.search(query) and re.search(_SCORE, query, re.IGNORECASE))


def fico_what_if(session: Session, query: str, loan_id: str | None = None) -> str:
    match = _REQUEST.fullmatch(query)
    if match is None:
        return ("For a local what-if, send one aggregate FICO value, for example: "
                "'What if the FICO was 820?' This changes the loan's hypothetical aggregate only, "
                "not one account's score. Account-specific, multi-field and chained scenarios are not supported. "
                "No provider request was made and the saved result is unchanged.")
    score = Decimal(match["score"])
    if not FICO_MIN <= score <= FICO_MAX:
        return ("The hypothetical aggregate FICO must be between 300 and 900 inclusive. "
                "No scenario was evaluated. No provider request was made and the saved result is unchanged.")
    if session.status == "processing":
        return "Wait for the current review to finish, then ask your FICO what-if again. No provider request was made."
    report = session.report
    batch = report.underwriting if report is not None else None
    if session.status != "completed" or session.mode != "TEST" or batch is None or batch.mode != "TEST":
        return ("A FICO what-if needs a completed TEST loan review. Review the loan in TEST mode first. "
                "No production decision, source data or saved result was changed. No provider request was made.")
    selected_id = loan_id or session.selected_loan_id
    if selected_id is None and len(batch.loan_results) == 1:
        selected_id = batch.loan_results[0].loan_id
    loan = next((item for item in batch.loan_results if item.loan_id == selected_id), None)
    if loan is None:
        return ("Select and review one loan before asking a FICO what-if. "
                "No other loan's data was used and no provider request was made.")
    data = loan.normalized_data.get("demo_decision")
    baseline = bounded_demo_context(data, mode=loan.mode, allowed_refs=set())
    if baseline is None:
        return ("This saved loan has no supported TEST decision snapshot. Reprocess the loan first, then ask the what-if. "
                "No provider request was made and the saved result is unchanged.")
    if (not isinstance(data.get("normalized_facts"), dict)
            or not isinstance(data.get("criteria"), list)
            or not isinstance(data.get("missing_inputs"), list)
            or any(not isinstance(item, dict) or not isinstance(item.get("criterion"), str)
                   or item.get("status") not in {"PASS", "FAIL", "UNKNOWN", "OBSERVED"}
                   or type(item.get("knockout")) is not bool for item in data["criteria"])):
        return ("This saved TEST snapshot has incomplete decision checks. Reprocess the loan before a what-if. "
                "No provider request was made and the saved result is unchanged.")
    # Work from the exact saved snapshot, not today's DB, source rows or earlier scenarios.
    facts = copy.deepcopy(data["normalized_facts"])
    checks = copy.deepcopy(data["criteria"])
    knockouts = {item["criterion"]: item for item in checks if item.get("knockout")}
    if set(knockouts) != {"bankruptcy_clear", "fico", "sbss"} or len(
            [item for item in checks if item.get("knockout")]) != 3:
        return ("This saved TEST snapshot has incomplete decision checks. Reprocess the loan before a what-if. "
                "No provider request was made and the saved result is unchanged.")
    facts["fico"] = str(score)
    knockouts["fico"].update(value=str(score), status="PASS" if score >= 650 else "FAIL", evidence_refs=[])
    identity = "explicit_unique_borrower_account_relationship" not in data.get("missing_inputs", [])
    result = decide_demo_facts(facts, checks, identity=identity)
    old_fico = baseline["normalized_facts"].get("fico", "Unknown")
    old_sbss = baseline["normalized_facts"].get("sbss", "Unknown")
    answer = (
        f"## What-if: aggregate FICO {score}\n\n"
        f"Loan **{loan.loan_id}**: **{result['label']}** (hypothetical TEST result).\n\n"
        f"- Aggregate FICO: **{old_fico} -> {score}**. This overrides the aggregate, not one linked account.\n"
        f"- FICO knockout: **{knockouts['fico']['status']}** ({score} {'>=' if score >= 650 else '<'} 650).\n"
        f"- SBSS remains **{old_sbss}**; all other saved inputs are unchanged.\n"
        f"- Saved result: **{baseline['label']}**. Hypothetical result: **{result['label']}**.\n\n"
        f"**Why:** {result['rationale']}\n\n"
        f"**Branch:** {result['branch']}."
    )
    if result["candidate_route"]:
        answer += f" **Route:** {result['candidate_route']}."
    if result["label"] == "Refer":
        unresolved = [*result["missing_inputs"], *result["conflicts"]]
        if unresolved:
            answer += "\n\n**Unresolved decision-flow inputs:** " + ", ".join(unresolved) + "."
    return answer + ("\n\nEvaluated locally against the saved TEST decision flow. Each what-if starts from the saved "
                     "baseline, not a previous scenario. Source files, stored scores and the original report are unchanged. "
                     "Simulation only; no lending action. No Claude request was made (0 provider tokens).")
