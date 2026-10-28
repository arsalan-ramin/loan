"""Prompt ownership and byte-for-byte compatibility without provider requests."""

import ast
import hashlib
from pathlib import Path

import pytest

from backend import llm, loan_review, prompts


# SHA-256 snapshots of the runtime strings before extraction, including whitespace.
@pytest.mark.parametrize("name,digest", [
    ("SYSTEM", "7a32923c79885a9a044f846870274c71beec603d6c9fcac53f87a487c9f7fc19"),
    ("V4_SYSTEM", "645299dff12a54ec719b2363cafcaba953d942b600c7653485a5cb8190d7f34a"),
    ("REVIEW_REASONING_GUIDANCE", "f722a5efbc79e95f941ee115aa87bc30153fac21d5e8a27ea428c1e15047724a"),
    ("CHAT_FOLLOW_UP_INSTRUCTIONS", "d52f502448948d0cba8d7da028d11c5cf4c63d7c5928bad8bfc0646a29a65fd4"),
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


def test_legacy_prompt_imports_remain_compatible():
    assert llm.SYSTEM is prompts.SYSTEM
    assert llm.V4_SYSTEM is prompts.V4_SYSTEM
    assert llm.REVIEW_REASONING_GUIDANCE is prompts.REVIEW_REASONING_GUIDANCE
    assert loan_review.CONTEXT_NOTE is prompts.SELECTED_LOAN_CONTEXT_NOTE


@pytest.mark.parametrize("module,expected_calls", [(llm, 4), (loan_review, 1)])
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
        components = list(ast.walk(call.args[0]))
        assert not any(isinstance(node, ast.Constant) for node in components)
        names = {node.id for node in components if isinstance(node, ast.Name)}
        assert names and names <= imported_prompts
        assert all(getattr(module, name) is getattr(prompts, name) for name in names)