"""Bounded transport retries: mocked SDK and sleep only, never paid requests."""

import logging
from types import SimpleNamespace
from unittest.mock import Mock, call
from uuid import UUID

import anthropic
import httpx
import pytest

from backend.commentary import explain
from backend.llm import AnthropicLLM, LLMError
from backend.models import Report
from backend.tests.test_underwriting import document, rows
from backend.underwriting_engine import evaluate_underwriting


PRIVATE = "PRIVATE_RETRY_SENTINEL"


@pytest.fixture
def sdk(monkeypatch, caplog):
    monkeypatch.setenv("ANTHROPIC_API_KEY", PRIVATE + "_KEY")
    monkeypatch.setenv("ANTHROPIC_MODEL", PRIVATE + "_MODEL")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid/" + PRIVATE)
    monkeypatch.delenv("ANTHROPIC_MAX_RETRIES", raising=False)
    sleep = Mock()
    monkeypatch.setattr("backend.llm.time.sleep", sleep)
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    response = SimpleNamespace(content=[SimpleNamespace(type="text", text=PRIVATE + "_RESPONSE")],
                               stop_reason="end_turn", usage=None)
    client.messages.create.return_value = response
    factory = Mock(return_value=client)
    monkeypatch.setattr("backend.llm.anthropic.Anthropic", factory)
    caplog.set_level(logging.INFO, logger="backend.llm")
    return client, response, sleep, factory


def failure(kind, retry_after=None):
    request = httpx.Request("POST", "https://example.invalid/" + PRIVATE)
    if kind == "timeout":
        return anthropic.APITimeoutError(request=request)
    if kind == "connection":
        return anthropic.APIConnectionError(message=PRIVATE, request=request)
    error_type = {401: anthropic.AuthenticationError, 403: anthropic.PermissionDeniedError,
                  404: anthropic.NotFoundError, 429: anthropic.RateLimitError}.get(kind, anthropic.APIStatusError)
    headers = {"x-private": PRIVATE}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return error_type(PRIVATE, response=httpx.Response(kind, request=request, headers=headers),
                      body={"private": PRIVATE})


def invoke():
    return AnthropicLLM().complete(PRIVATE + "_SYSTEM", PRIVATE + "_PROMPT")


def assert_logs(caplog, attempts, retries, terminal, category, exhausted=False):
    records = [record for record in caplog.records if record.name == "backend.llm"]
    assert len(records) == retries + 2
    messages = [record.getMessage() for record in records]
    request_id = messages[0].split("request_id=", 1)[1]
    assert UUID(request_id).version == 4
    assert messages[0].startswith("llm_invocation_started ")
    for attempt, message in enumerate(messages[1:-1], 1):
        assert message.startswith("llm_invocation_retry ")
        assert f"attempt={attempt} category={category} delay_seconds=" in message
    assert messages[-1].startswith(f"llm_invocation_{terminal} ")
    assert f"attempts={attempts} retry_exhausted={exhausted}" in messages[-1]
    for record, message in zip(records, messages):
        assert f"request_id={request_id}" in message
        assert PRIVATE not in repr(record.__dict__)
        assert record.exc_info is None
        assert record.stack_info is None


@pytest.mark.parametrize("kind,category", [
    ("connection", "connection"), ("timeout", "timeout"), (429, "rate_limit"),
    (408, "api_status"), (409, "api_status"), (500, "api_status"),
    (502, "api_status"), (503, "api_status"), (529, "api_status"), (599, "api_status"),
])
def test_transient_failure_then_success(sdk, caplog, kind, category):
    client, response, sleep, factory = sdk
    client.messages.create.side_effect = [failure(kind), response]
    assert invoke() == PRIVATE + "_RESPONSE"
    assert client.messages.create.call_count == 2
    assert client.messages.create.call_args_list[0] == client.messages.create.call_args_list[1]
    sleep.assert_called_once_with(0.5)
    assert factory.call_count == 1
    assert factory.call_args.kwargs["max_retries"] == 0
    assert factory.call_args.kwargs["timeout"] == 60.0
    assert_logs(caplog, 2, 1, "completed", category)


@pytest.mark.parametrize("kind,category,status", [
    ("connection", "connection", 503), ("timeout", "timeout", 504), (429, "rate_limit", 429),
    (408, "api_status", 502), (409, "api_status", 502), (500, "api_status", 502),
    (529, "api_status", 502),
])
def test_default_budget_exhaustion_is_safe(sdk, caplog, kind, category, status):
    client, _, sleep, _ = sdk
    client.messages.create.side_effect = failure(kind)
    with pytest.raises(LLMError) as caught:
        invoke()
    assert caught.value.status_code == status
    assert caught.value.__suppress_context__
    assert PRIVATE not in str(caught.value)
    assert client.messages.create.call_count == 3
    assert sleep.call_args_list == [call(0.5), call(1.0)]
    assert_logs(caplog, 3, 2, "failed", category, exhausted=True)


@pytest.mark.parametrize("status", [300, 400, 401, 403, 404, 405, 410, 418, 422])
def test_nonretryable_status_fails_immediately(sdk, caplog, status):
    client, _, sleep, _ = sdk
    client.messages.create.side_effect = failure(status)
    with pytest.raises(LLMError):
        invoke()
    client.messages.create.assert_called_once()
    sleep.assert_not_called()
    assert_logs(caplog, 1, 0, "failed", "unused")


@pytest.mark.parametrize("value,retries", [
    (None, 2), ("0", 0), ("1", 1), ("2", 2), ("5", 5), ("99", 5),
    ("-1", 0), (" 3 ", 3), ("", 2), ("bad", 2), ("1.5", 2),
    ("nan", 2), ("inf", 2), ("9" * 5000, 2),
])
def test_configured_budget_and_capped_exponential_backoff(sdk, monkeypatch, caplog, value, retries):
    client, _, sleep, _ = sdk
    if value is not None:
        monkeypatch.setenv("ANTHROPIC_MAX_RETRIES", value)
    llm = AnthropicLLM()
    assert llm.max_retries == retries
    client.messages.create.side_effect = failure("connection")
    with pytest.raises(LLMError):
        llm.complete(PRIVATE, PRIVATE)
    assert client.messages.create.call_count == retries + 1
    assert sleep.call_args_list == [call(delay) for delay in [0.5, 1.0, 2.0, 4.0, 4.0][:retries]]
    assert_logs(caplog, retries + 1, retries, "failed", "connection", exhausted=True)


@pytest.mark.parametrize("header,delay", [
    (None, 0.5), ("", 0.5), ("bad", 0.5), (PRIVATE, 0.5),
    ("-1", 0.5), ("NaN", 0.5), ("inf", 0.5), ("-inf", 0.5), ("1e9999", 0.5),
    ("0", 0.0), ("0.25", 0.25), (" 2.5 ", 2.5), ("30", 30.0), ("300", 30.0),
])
def test_retry_after_is_optional_safe_and_bounded(sdk, caplog, header, delay):
    client, response, sleep, _ = sdk
    client.messages.create.side_effect = [failure(429, header), response]
    invoke()
    sleep.assert_called_once_with(delay)
    assert_logs(caplog, 2, 1, "completed", "rate_limit")


def test_retry_after_is_used_for_server_status(sdk):
    client, response, sleep, _ = sdk
    client.messages.create.side_effect = [failure(529, "3"), response]
    invoke()
    sleep.assert_called_once_with(3.0)


def test_retryable_then_nonretryable_does_not_exhaust_budget(sdk, caplog):
    client, _, sleep, _ = sdk
    client.messages.create.side_effect = [failure("connection"), failure(400)]
    with pytest.raises(LLMError):
        invoke()
    assert client.messages.create.call_count == 2
    sleep.assert_called_once_with(0.5)
    assert_logs(caplog, 2, 1, "failed", "connection")


@pytest.mark.parametrize("stop,text", [("max_tokens", "partial"), ("pause_turn", "partial"), ("end_turn", " ")])
def test_response_failures_after_transport_retry_do_not_retry_again(sdk, caplog, stop, text):
    client, response, sleep, _ = sdk
    response.stop_reason = stop
    response.content[0].text = text
    client.messages.create.side_effect = [failure("connection"), response]
    with pytest.raises(LLMError):
        invoke()
    assert client.messages.create.call_count == 2
    sleep.assert_called_once_with(0.5)
    assert_logs(caplog, 2, 1, "failed", "connection")


@pytest.mark.parametrize("text", ["not JSON", "{}"])
def test_invalid_completed_json_is_not_retried_by_transport_or_business_validation(sdk, text):
    client, response, sleep, _ = sdk
    response.content[0].text = text
    report = Report(decision="needs_review", summary="Review", findings=[], document_count=1,
                    underwriting=evaluate_underwriting([document(rows())]))
    with pytest.raises(LLMError):
        explain(AnthropicLLM(), report)
    client.messages.create.assert_called_once()
    sleep.assert_not_called()