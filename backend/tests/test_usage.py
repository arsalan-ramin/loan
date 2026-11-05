"""Provider usage tests use a synthetic HTTP transport, never paid requests."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from backend.agents import MemoryAgent
from backend.batch_review import BatchReviewer
from backend.llm import AnthropicLLM, LLMError
from backend.main import create_app
from backend.tests.test_batch_review import claude_output
from backend.tests.test_interactive_chat import sources
from backend.usage import CURRENT_USAGE, UsageLedger, usage_scope


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-key")
    monkeypatch.setattr("backend.llm.time.sleep", Mock())
    original = anthropic.Anthropic
    settings = {"status": 200, "stop": "end_turn", "calls": 0}

    def handle(request):
        settings["calls"] += 1
        statuses = settings.get("statuses")
        status = statuses.pop(0) if statuses else settings["status"]
        if status != 200:
            return httpx.Response(status, json={"type": "error", "error": {
                "type": "api_error", "message": "PRIVATE_PROVIDER_BODY"}})
        body = json.loads(request.content)
        prompt = body["messages"][0]["content"]
        if prompt.startswith("{"):
            text = claude_output(json.loads(prompt))
        else:
            text = "Grounded answer."
        return httpx.Response(200, json={
            "id": "msg_synthetic", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": text}], "stop_reason": settings["stop"],
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_creation_input_tokens": 3, "cache_read_input_tokens": 7},
        })

    def factory(**kwargs):
        return original(api_key=kwargs["api_key"], max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(handle)))

    monkeypatch.setattr("backend.llm.anthropic.Anthropic", factory)
    return settings


def test_provider_counts_caches_and_persists_without_content(tmp_path, transport):
    ledger = UsageLedger(tmp_path)
    with usage_scope(ledger=ledger, session_id="s1", loan_id="L1", operation="chat"):
        assert AnthropicLLM().complete("PRIVATE_SYSTEM", "PRIVATE_PROMPT") == "Grounded answer."
    page = UsageLedger(tmp_path).page()
    row = page["requests"][0]
    assert row["input_tokens"] == 10 and row["output_tokens"] == 5
    assert row["cache_creation_input_tokens"] == 3 and row["cache_read_input_tokens"] == 7
    assert row["total_tokens"] == page["totals"]["total_tokens"] == 25
    assert row["status"] == "succeeded" and row["attempts"] == 1
    assert row["session_id"] == "s1" and row["loan_id"] == "L1" and row["operation"] == "chat"
    assert row["duration_ms"] >= 0
    assert "PRIVATE_" not in json.dumps(page)
    assert CURRENT_USAGE.get().ledger is None


@pytest.mark.parametrize("stop", ["max_tokens", "pause_turn"])
def test_discarded_provider_text_still_counts_tokens(tmp_path, transport, stop):
    transport["stop"] = stop
    ledger = UsageLedger(tmp_path)
    with usage_scope(ledger=ledger), pytest.raises(LLMError):
        AnthropicLLM().complete("system", "prompt")
    row = ledger.page()["requests"][0]
    assert row["status"] == "failed" and row["total_tokens"] == 25


def test_failure_retains_unknown_usage_and_attempt_count(tmp_path, transport):
    transport["status"] = 500
    ledger = UsageLedger(tmp_path)
    with usage_scope(ledger=ledger), pytest.raises(LLMError):
        AnthropicLLM().complete("system", "prompt")
    page = ledger.page()
    row = page["requests"][0]
    assert row["input_tokens"] is None and row["total_tokens"] is None
    assert row["attempts"] == transport["calls"] == 3
    assert row["category"] == "api_status" and row["status"] == "failed"
    assert page["totals"]["reported_requests"] == 0
    assert page["totals"]["unreported_requests"] == 1


def test_retry_success_is_one_invocation_with_all_attempts(tmp_path, transport):
    transport["statuses"] = [500, 200]
    ledger = UsageLedger(tmp_path)
    with usage_scope(ledger=ledger):
        AnthropicLLM().complete("system", "prompt")
    page = ledger.page()
    assert page["total"] == 1 and page["totals"]["total_tokens"] == 25
    assert page["requests"][0]["attempts"] == 2


def test_missing_configuration_records_no_provider_attempt(tmp_path, transport, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    ledger = UsageLedger(tmp_path)
    with usage_scope(ledger=ledger), pytest.raises(LLMError):
        AnthropicLLM().complete("system", "prompt")
    row = ledger.page()["requests"][0]
    assert row["attempts"] == transport["calls"] == 0
    assert row["category"] == "not_configured"


def test_failed_accounting_prevents_provider_call(tmp_path, transport, monkeypatch):
    ledger = UsageLedger(tmp_path)
    monkeypatch.setattr(ledger, "start", Mock(side_effect=sqlite3.OperationalError("locked")))
    with usage_scope(ledger=ledger), pytest.raises(LLMError, match="No provider request"):
        AnthropicLLM().complete("system", "prompt")
    assert transport["calls"] == 0


def test_failed_accounting_after_provider_is_explicit(tmp_path, transport, monkeypatch):
    ledger = UsageLedger(tmp_path)
    monkeypatch.setattr(ledger, "finish", Mock(side_effect=sqlite3.OperationalError("locked")))
    with usage_scope(ledger=ledger), pytest.raises(LLMError, match="may have incurred charges"):
        AnthropicLLM().complete("system", "prompt")
    assert transport["calls"] == 1


def test_concurrent_scopes_pagination_and_session_totals(tmp_path, transport):
    ledger = UsageLedger(tmp_path)

    def invoke(index):
        with usage_scope(ledger=ledger, session_id=f"s{index % 2}", loan_id=f"L{index}"):
            AnthropicLLM().complete("system", "prompt")

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(invoke, range(10)))
    assert ledger.page()["totals"]["total_tokens"] == 250
    page = ledger.page("s1", offset=2, limit=2)
    assert page["total"] == 5 and len(page["requests"]) == 2
    assert page["totals"]["total_tokens"] == 125
    assert all(row["session_id"] == "s1" for row in page["requests"])


def test_recovery_preserves_unknown_usage(tmp_path):
    ledger = UsageLedger(tmp_path)
    ledger.start("interrupted", "model", CURRENT_USAGE.get())
    ledger.recover_interrupted()
    row = ledger.page()["requests"][0]
    assert row["status"] == "interrupted" and row["total_tokens"] is None
    assert ledger.page()["totals"]["failed_requests"] == 1


def test_api_correlates_chat_and_keeps_totals_after_restart(tmp_path, transport):
    root = tmp_path / "data"
    with TestClient(create_app(root)) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        assert client.post(f"/api/sessions/{sid}/messages", json={"content": "hello"}).status_code == 200
        assert client.get("/api/usage").json()["total"] == 0
        reply = client.post(f"/api/sessions/{sid}/messages", json={"content": "What can you review?"})
        assert reply.status_code == 200
        page = client.get("/api/usage", params={"session_id": sid}).json()
        row = page["requests"][0]
        assert row["http_request_id"] == reply.headers["X-Request-ID"]
        assert row["session_id"] == sid and row["operation"] == "chat"
        assert page["totals"]["total_tokens"] == 25
        assert client.get("/api/usage?offset=-1").status_code == 422
        assert client.get("/api/usage?limit=101").status_code == 422
        assert client.get("/api/usage", params={"session_id": "other"}).json()["total"] == 0
    with TestClient(create_app(root)) as client:
        assert client.get("/api/usage").json()["totals"]["total_tokens"] == 25


def test_worker_and_selected_review_are_metered(tmp_path, transport, monkeypatch):
    monkeypatch.setattr(MemoryAgent, "index", lambda self, session: "Synthetic memory")
    with TestClient(create_app(tmp_path / "data")) as client:
        files = []
        for name, raw in sources():
            path = tmp_path / name
            path.write_bytes(raw)
            files.append((name, path))
        assert client.app.state.dataset.replace(files)
        futures = []
        executor = client.app.state.executor

        def submit(*args):
            future = ThreadPoolExecutor.submit(executor, *args)
            futures.append(future)
            return future

        monkeypatch.setattr(executor, "submit", submit)
        sid = client.post("/api/sessions", json={}).json()["id"]
        started = client.post(f"/api/sessions/{sid}/messages", json={"content": "L1"})
        futures[0].result(timeout=15)
        completed = client.get(f"/api/sessions/{sid}").json()
        assert completed["status"] == "completed", completed["error"]
        usage = client.get("/api/usage").json()["requests"][0]
        assert usage["operation"] == "report_explanation" and usage["loan_id"] == "L1"
        assert usage["http_request_id"] == started.headers["X-Request-ID"]
        response = client.post(f"/api/sessions/{sid}/assistant/loan-review", json={
            "confirmed": True, "loan_id": "L1", "report_id": completed["report"]["id"],
            "expected_updated_at": completed["updated_at"],
        })
        assert response.status_code == 200, response.text
        page = client.get("/api/usage", params={"session_id": sid}).json()
        assert page["total"] == 2 and page["totals"]["total_tokens"] == 50
        assert page["requests"][0]["operation"] == "selected_loan_review"


def test_batch_usage_is_separate_from_replaceable_results(tmp_path, transport):
    from backend.loan_dataset import LoanDataset

    dataset = LoanDataset(tmp_path / "data")
    files = []
    for name, raw in sources():
        path = tmp_path / name
        path.write_bytes(raw)
        files.append((name, path))
    assert dataset.replace(files)
    reviewer = BatchReviewer(dataset, concurrency=2)
    reviewer.start()
    reviewer._thread.join(timeout=15)
    assert not reviewer.running()
    assert reviewer.status()["status"] == "completed"
    page = reviewer.usage.page()
    assert page["total"] == 2 and page["totals"]["total_tokens"] == 50
    assert {row["loan_id"] for row in page["requests"]} == {"L1", "L2"}
    assert all(row["batch_id"] and row["session_id"] is None
               and row["operation"] == "batch_loan_review" for row in page["requests"])
    dataset._clear(status="empty")
    assert reviewer.usage.page()["totals"]["total_tokens"] == 50
