"""Content-free transport diagnostics; all SDK calls are mocked, never billable."""

import logging
import re
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import anthropic
import httpx
import pytest

from backend.llm import AnthropicLLM, LLMError


SENTINELS = {
    name: f"PRIVATE_{name}_SENTINEL" for name in
    ("KEY", "MODEL", "URL", "SYSTEM", "PROMPT", "RESPONSE", "EXCEPTION", "STOP", "USAGE")
}


@pytest.fixture
def sdk(monkeypatch, caplog):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINELS["KEY"])
    monkeypatch.setenv("ANTHROPIC_MAX_RETRIES", "0")
    monkeypatch.setattr("backend.llm.time.sleep", Mock())
    monkeypatch.setenv("ANTHROPIC_MODEL", SENTINELS["MODEL"])
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid/" + SENTINELS["URL"])
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=SENTINELS["RESPONSE"])],
        stop_reason="end_turn", usage=SimpleNamespace(input_tokens=12, output_tokens=7),
    )
    factory = Mock(return_value=client)
    monkeypatch.setattr("backend.llm.anthropic.Anthropic", factory)
    caplog.set_level(logging.INFO, logger="backend.llm")
    return client, factory


def invoke():
    return AnthropicLLM().complete(SENTINELS["SYSTEM"], SENTINELS["PROMPT"])


def assert_safe_pair(caplog, terminal, category):
    records = [record for record in caplog.records if record.name == "backend.llm"]
    assert len(records) == 2
    start, end = [record.getMessage() for record in records]
    assert start.startswith("llm_invocation_started ")
    assert end.startswith(f"llm_invocation_{terminal} ")
    request_id = start.split("request_id=", 1)[1]
    assert str(UUID(request_id)) == request_id
    assert UUID(request_id).version == 4
    assert f"request_id={request_id} " in end
    assert f"category={category} " in end
    duration = re.search(r"duration_ms=([0-9.]+) ", end)
    assert duration is not None and float(duration.group(1)) >= 0
    for record in records:
        assert record.exc_info is None
        assert record.stack_info is None
        for sentinel in SENTINELS.values():
            assert sentinel not in repr(record.__dict__)
    for sentinel in SENTINELS.values():
        assert sentinel not in caplog.text
    return end


@pytest.mark.parametrize("stop", ["end_turn", "stop_sequence"])
def test_success_is_content_free_and_preserves_sdk_payload(sdk, caplog, stop):
    client, factory = sdk
    client.messages.create.return_value.stop_reason = stop
    assert invoke() == SENTINELS["RESPONSE"]
    end = assert_safe_pair(caplog, "completed", "success")
    assert f"response_chars={len(SENTINELS['RESPONSE'])} " in end
    assert f"stop_category={stop} " in end
    assert "input_tokens=12 output_tokens=7" in end
    factory.assert_called_once_with(api_key=SENTINELS["KEY"],
        base_url="https://example.invalid/" + SENTINELS["URL"], timeout=60.0, max_retries=0)
    client.messages.create.assert_called_once_with(model=SENTINELS["MODEL"], max_tokens=8192,
        system=SENTINELS["SYSTEM"], messages=[{"role": "user", "content": SENTINELS["PROMPT"]}])


@pytest.mark.parametrize("error_type,category,status,message", [
    (anthropic.AuthenticationError, "authentication", 503, "authentication failed"),
    (anthropic.PermissionDeniedError, "model_unavailable", 503, "unavailable to this account"),
    (anthropic.NotFoundError, "model_unavailable", 503, "unavailable to this account"),
    (anthropic.RateLimitError, "rate_limit", 429, "rate limit reached"),
    (anthropic.APITimeoutError, "timeout", 504, "timed out"),
    (anthropic.APIConnectionError, "connection", 503, "Unable to connect"),
    (anthropic.APIStatusError, "api_status", 502, "rejected the request"),
])
def test_transport_failures_log_only_fixed_categories(sdk, caplog, error_type, category, status, message):
    client, _ = sdk
    request = httpx.Request("POST", "https://example.invalid/" + SENTINELS["URL"])
    if issubclass(error_type, anthropic.APIStatusError):
        error = error_type(SENTINELS["EXCEPTION"], response=httpx.Response(599, request=request),
                           body={"private": SENTINELS["RESPONSE"]})
    elif error_type is anthropic.APITimeoutError:
        error = error_type(request=request)
    else:
        error = error_type(message=SENTINELS["EXCEPTION"], request=request)
    client.messages.create.side_effect = error
    with pytest.raises(LLMError, match=message) as caught:
        invoke()
    assert caught.value.status_code == status
    assert caught.value.__suppress_context__
    end = assert_safe_pair(caplog, "failed", category)
    assert f"http_status={status} " in end
    assert "599" not in end  # Never read arbitrary provider status attributes.
    assert "response_chars=0 stop_category=not_received" in end
    client.messages.create.assert_called_once()


@pytest.mark.parametrize("stop,text,category,message", [
    ("max_tokens", SENTINELS["RESPONSE"], "truncation", "8,192-token output limit"),
    (SENTINELS["STOP"], SENTINELS["RESPONSE"], "abnormal_stop", "before a normal completion"),
    ("end_turn", "   ", "empty_response", "returned no text"),
])
def test_response_failures_keep_content_and_unknown_stop_private(sdk, caplog, stop, text, category, message):
    client, _ = sdk
    response = client.messages.create.return_value
    response.stop_reason = stop
    response.content[0].text = text
    with pytest.raises(LLMError, match=message) as caught:
        invoke()
    assert caught.value.status_code == 502
    end = assert_safe_pair(caplog, "failed", category)
    expected_stop = "other" if stop == SENTINELS["STOP"] else stop
    assert f"stop_category={expected_stop} " in end
    assert f"response_chars={len(text.strip())} " in end
    assert "input_tokens=12 output_tokens=7" in end
    client.messages.create.assert_called_once()  # No extra billable retry.


@pytest.mark.parametrize("value", [None, True, False, -1, 1.5, SENTINELS["USAGE"], Mock()])
def test_invalid_token_counts_are_not_logged(sdk, caplog, value):
    client, _ = sdk
    client.messages.create.return_value.usage = SimpleNamespace(input_tokens=value, output_tokens=value)
    invoke()
    end = assert_safe_pair(caplog, "completed", "success")
    assert "input_tokens=None output_tokens=None" in end


@pytest.mark.parametrize("usage", [None, SimpleNamespace(), SimpleNamespace(input_tokens=0, output_tokens=0)])
def test_usage_is_optional_and_zero_is_valid(sdk, caplog, usage):
    client, _ = sdk
    client.messages.create.return_value.usage = usage
    invoke()
    end = assert_safe_pair(caplog, "completed", "success")
    expected = 0 if hasattr(usage, "input_tokens") else None
    assert f"input_tokens={expected} output_tokens={expected}" in end


def test_missing_key_logs_without_sdk_call(sdk, caplog, monkeypatch):
    _, factory = sdk
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(LLMError, match="Anthropic is not configured") as caught:
        invoke()
    assert caught.value.status_code == 503
    assert "http_status=503 " in assert_safe_pair(caplog, "failed", "not_configured")
    factory.assert_not_called()


def test_request_ids_are_unique(sdk, caplog):
    invoke()
    first = assert_safe_pair(caplog, "completed", "success").split("request_id=", 1)[1].split()[0]
    caplog.clear()
    invoke()
    second = assert_safe_pair(caplog, "completed", "success").split("request_id=", 1)[1].split()[0]
    assert first != second


def test_duration_uses_monotonic_elapsed_milliseconds(sdk, caplog, monkeypatch):
    monkeypatch.setattr("backend.llm.time", SimpleNamespace(perf_counter=Mock(side_effect=[10.0, 10.125])))
    invoke()
    assert "duration_ms=125.000 " in assert_safe_pair(caplog, "completed", "success")