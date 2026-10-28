"""Prompt ownership and byte-for-byte compatibility without provider requests."""

import ast
import hashlib
from pathlib import Path

import pytest

from backend import commentary, llm, llm_context, loan_review, prompts


# SHA-256 runtime snapshots, including the explicitly versioned TEST demo update.
@pytest.mark.parametrize("name,digest", [
    ("DEMO_SYSTEM", "458bbfc34ed2163e170162e65e4038f2f6680b532eaee73d8b792acddba22d2b"),
    ("DEMO_AGGREGATE_INSTRUCTIONS", "4fd1bac306fbfb8301df268b35a70b1ddd2f8c4f4b238a435ba4094b51342a22"),
    ("DEMO_SELECTED_LOAN_INSTRUCTIONS", "e363a50540076ee7c8a669c017aeaf8ed495ff3857c6e8c892287d23237b2e50"),
    ("DEMO_SELECTED_LOAN_CONTEXT_NOTE", "d34a861b02ad0ea91c2ac40ef878ec29eda611bcd844a9d93410cc0ed4be81e3"),
    ("SYSTEM", "7a32923c79885a9a044f846870274c71beec603d6c9fcac53f87a487c9f7fc19"),
    ("V4_SYSTEM", "a357a49510aaff807e48008ecec9b33155119ff0ca6c5040ebf5d3ae16bec71e"),
    ("REVIEW_REASONING_GUIDANCE", "c39b743957d5ea91c469480085287e96ec86c71adac4c41257644be968164e8d"),
    ("CHAT_FOLLOW_UP_INSTRUCTIONS", "9702c259a4360dc628b46db39e6327a024adcc0233f871ad5c2edb91c3d8540b"),
    ("CHAT_WORKFLOW_INSTRUCTIONS", "db37725ecda6d84a10e62ef6a5e9b685934a0bb673fb3f5b166840cd6096528a"),
    ("AGGREGATE_UNDERWRITING_INSTRUCTIONS", "5787dd958d3c74a6d8e60dc6234b13c1a30ba7f1acc9eaa931dbd17878f21457"),
    ("LEGACY_POLICY_EXPLANATION_INSTRUCTIONS", "355f91a2b2ccd307d67e19e2fa323dc92a77258688fc63f5366c2728769d0af1"),
    ("GENERIC_EXPLANATION_INSTRUCTIONS", "353a02a23f4c4059fa88cd1741ec10b346803096712993a274675218075f41b1"),
    ("SELECTED_LOAN_CONTEXT_NOTE", "972bafd4df566dd3fdb338333823be4c058672b15c80ab6850e1e9b56a4e98f5"),
    ("SELECTED_LOAN_EVIDENCE_PREFERENCE_INSTRUCTIONS", "e949ff7e21b2e503cb741051ee5c97a1a25ee47cfb41d4479bdd759b8c0a2de7"),
    ("SELECTED_LOAN_RECONCILIATION_NOTE", "1fb14a45fbd5eea7c15e2f43100fec407c08028dfa747811c3211831072ea444"),
    ("SELECTED_LOAN_REVIEW_INSTRUCTIONS", "9eebf171312b517d68bd41a8e21e42795822d41ba105a73a9ab9c26de2068d36"),
])
def test_prompt_runtime_text_is_unchanged(name, digest):
    assert hashlib.sha256(getattr(prompts, name).encode("utf-8")).hexdigest() == digest


def test_prompt_consumers_import_from_prompt_owner():
    assert commentary.SYSTEM is prompts.SYSTEM
    assert commentary.V4_SYSTEM is prompts.V4_SYSTEM
    assert commentary.REVIEW_REASONING_GUIDANCE is prompts.REVIEW_REASONING_GUIDANCE
    assert llm_context.CHAT_WORKFLOW_INSTRUCTIONS is prompts.CHAT_WORKFLOW_INSTRUCTIONS
    assert loan_review.CONTEXT_NOTE is prompts.SELECTED_LOAN_CONTEXT_NOTE
    assert commentary.DEMO_SYSTEM is loan_review.DEMO_SYSTEM is prompts.DEMO_SYSTEM
    assert commentary.DEMO_AGGREGATE_INSTRUCTIONS is prompts.DEMO_AGGREGATE_INSTRUCTIONS
    assert loan_review.DEMO_SELECTED_LOAN_INSTRUCTIONS is prompts.DEMO_SELECTED_LOAN_INSTRUCTIONS


def test_explanations_name_requirements_instead_of_scope_jargon():
    guidance = prompts.REVIEW_REASONING_GUIDANCE
    assert "State each unmet or unverified requirement specifically" in guidance
    assert "not an additional credit-decision rule" in guidance
    assert "explicit group credit <=USD250K" in guidance


def test_explanation_does_not_repeat_ui_disclaimers():
    assert "do not append another disclaimer to the explanation" in prompts.REVIEW_REASONING_GUIDANCE
    assert "A simulated candidate is not a decision." not in prompts.V4_SYSTEM


def test_rationale_separates_observed_risk_from_blockers_and_stale_snapshots():
    guidance = prompts.REVIEW_REASONING_GUIDANCE
    assert "A missing input is not\na failed credit criterion" in guidance
    assert "SAVED_REPORT_INCONSISTENT_REPROCESS_REQUIRED" in guidance
    assert "do not assert that missing dates block all four automatically" in guidance
    assert "Only use FICO when report_selection.fico_usable_in_commentary is true" in guidance
    assert "per-check reconciliation holds the full audit" in guidance


def test_test_demo_explained_first_without_changing_strict_or_operational_contract():
    guidance = prompts.REVIEW_REASONING_GUIDANCE
    assert "explain it FIRST" in guidance
    assert "FIRST_ACCOUNT_MATCH_FOR_TEST_DEMO" in guidance
    assert "Do not mix strict selection diagnostics" in guidance
    assert "UNKNOWN\nnever clears Approve" in guidance
    assert "decision_recommendation null" in guidance
    assert "Report dates are not a demo selection prerequisite" in guidance


@pytest.mark.parametrize("module,expected_calls", [(llm, 0), (llm_context, 0), (commentary, 4), (loan_review, 1)])
def test_system_prompt_components_are_owned_by_prompts(module, expected_calls):
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported_prompts = {
        alias.asname or alias.name
        for node in tree.body if isinstance(node, ast.ImportFrom) and node.module == "prompts"
        for alias in node.names
    }
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == "complete"]
    assert len(calls) == expected_calls
    for call in calls:
        # Conditional routing is allowed, but both prompt branches must still
        # be composed exclusively of constants imported from the prompt owner.
        expressions = ([call.args[0].body, call.args[0].orelse]
                       if isinstance(call.args[0], ast.IfExp) else [call.args[0]])
        components = [node for expression in expressions for node in ast.walk(expression)]
        assert not any(isinstance(node, ast.Constant) for node in components)
        names = {node.id for node in components if isinstance(node, ast.Name)}
        assert names and names <= imported_prompts
        assert all(getattr(module, name) is getattr(prompts, name) for name in names)