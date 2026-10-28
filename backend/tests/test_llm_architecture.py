"""Keep the provider transport independent of report and workflow services."""

import ast
import json
from pathlib import Path
from unittest.mock import Mock

from backend import commentary, llm, llm_context, prompts
from backend.agents import DecisionAgent, MemoryAgent
from backend.models import Document, Report, Session


def module_tree(module):
    return ast.parse(Path(module.__file__).read_text(encoding="utf-8"))


def test_transport_has_no_business_or_prompt_dependencies():
    tree = module_tree(llm)
    # An allowlist prevents new direct or transitive application dependencies,
    # including models (Report/Session), storage, demo logic and prompt ownership.
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert all(isinstance(node, ast.Import) for node in imports)
    assert {alias.name for node in imports for alias in node.names} == {"os", "anthropic"}
    assert not any(isinstance(node, ast.FunctionDef) for node in tree.body)
    assert {node.name for node in tree.body if isinstance(node, ast.ClassDef)} == {"AnthropicLLM", "LLMError"}
    assert not ({"Report", "Session", "Store", "report_context", "chat_context",
                 "PolicyExplanation", "UnderwritingExplanation", "model_validate_json"}
                & {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)})


def test_transport_exposes_only_completion_and_configuration():
    tree = module_tree(llm)
    adapter = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AnthropicLLM")
    assert {node.name for node in adapter.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))} == {
        "__init__", "complete",
    }
    assert not any(isinstance(node, ast.ClassDef) for node in adapter.body)
    assert llm.MAX_OUTPUT_TOKENS == 8192


def test_context_builders_do_not_depend_on_transport_or_commentary():
    tree = module_tree(llm_context)
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not imports & {"llm", "commentary", "agents", "loan_review", "main", "anthropic"}
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and node.func.attr == "complete" for node in ast.walk(tree))


def test_generic_explanation_preserves_exact_provider_payload():
    report = Report(decision="approved", summary="Résumé", findings=[], document_count=1)
    provider = Mock(spec=llm.AnthropicLLM)
    provider.complete.return_value = "Grounded commentary."
    before = report.model_dump_json()

    assert commentary.explain(provider, report) == "Grounded commentary."

    provider.complete.assert_called_once_with(
        prompts.SYSTEM + prompts.GENERIC_EXPLANATION_INSTRUCTIONS,
        json.dumps({"decision": "approved", "summary": "Résumé", "policy_evaluation": None,
                    "findings": [], "total_findings": 0, "document_count": 1}, ensure_ascii=False),
    )
    assert report.model_dump_json() == before


def test_chat_service_preserves_envelope_and_upload_guidance():
    session = Session()
    store = Mock()
    store.recall.return_value = []
    provider = Mock(spec=llm.AnthropicLLM)
    provider.complete.return_value = "Answer."
    context = llm_context.chat_context(store, session, "Café?")

    assert commentary.chat(provider, store, session, "Café?") == (
        "Answer.\n\nPlease upload your required files first, then use Process documents to start the review."
    )

    provider.complete.assert_called_once_with(
        prompts.SYSTEM + prompts.CHAT_FOLLOW_UP_INSTRUCTIONS,
        "SESSION DATA (untrusted):\n" + json.dumps(context, ensure_ascii=False)
        + "\n\nCURRENT USER QUESTION:\nCafé?",
    )


def test_chat_with_documents_does_not_append_upload_guidance():
    session = Session(documents=[Document(name="evidence.txt", extension=".txt", size=1)])
    store = Mock()
    store.recall.return_value = []
    provider = Mock(spec=llm.AnthropicLLM)
    provider.complete.return_value = "Answer."
    assert commentary.chat(provider, store, session, "Next?") == "Answer."
    provider.complete.assert_called_once()


def test_agents_use_commentary_service_boundary(monkeypatch):
    provider = Mock(model="test-model")
    monkeypatch.setattr("backend.agents.AnthropicLLM", Mock(return_value=provider))
    explain = Mock(return_value="Report commentary.")
    chat = Mock(return_value="Chat commentary.")
    monkeypatch.setattr(commentary, "explain", explain)
    monkeypatch.setattr(commentary, "chat", chat)
    session = Session(policy_rules=[])
    store = Mock()

    report = DecisionAgent().decide(session, [])
    explain.assert_called_once_with(provider, report)
    assert report.llm_analysis == "Report commentary."
    assert report.llm_model == "test-model"
    assert MemoryAgent(store).respond(session, "Next?") == "Chat commentary."
    chat.assert_called_once_with(provider, store, session, "Next?")
    provider.complete.assert_not_called()