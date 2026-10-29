"""Strict commentary parsing and content-free, correlated validation diagnostics.

Only application-owned schema paths and error categories may cross the logging
boundary. Provider text, configuration, IDs, exception strings and values never do.
Transport failures remain owned by the transport; this module never retries.
"""

import json
import logging
import re
from time import perf_counter
from typing import Literal, NoReturn, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from .llm import LLMError

Output = TypeVar("Output", bound=BaseModel)
Stage = Literal["schema", "scope", "rules", "evidence", "reconciliation"]
Workflow = Literal["aggregate", "selected", "policy"]
_ERROR_TYPES = frozenset({
    "missing", "extra_forbidden", "string_type", "string_too_short", "string_too_long",
    "list_type", "dict_type", "model_type", "literal_error", "none_required",
    "float_type", "finite_number", "greater_than_equal", "less_than_equal",
    "too_long", "too_short", "bool_type", "json_invalid", "validation_error",
    "route_mismatch", "confidence_not_null", "unsupported_reference",
    "incomplete_or_duplicate", "mapped_without_evidence",
})
_SCHEMA_MESSAGES = {
    "aggregate": "Claude returned invalid structured underwriting commentary. Deterministic results remain available.",
    "selected": "Claude returned invalid selected-loan commentary. It was discarded; your report is unchanged.",
    "policy": "Claude returned invalid structured policy commentary. Deterministic policy results remain available.",
}


def _safe_path(model: type[BaseModel], loc: tuple, *, extra: bool = False) -> str:
    """Walk trusted schema edges, never echo arbitrary Pydantic location segments."""
    root = model.model_json_schema()
    node = root
    parts: list[str] = []
    for index, segment in enumerate(loc):
        if extra and index == len(loc) - 1:
            parts.append("<extra>")
            break
        if "$ref" in node:
            node = root.get("$defs", {}).get(node["$ref"].rsplit("/", 1)[-1], {})
        if node.get("type") == "array" and type(segment) is int:
            parts.append("[]")
            node = node.get("items", {})
        elif isinstance(segment, str) and segment in node.get("properties", {}):
            parts.append(segment)
            node = node["properties"][segment]
        else:
            parts.append("<unknown>")
            break
    return ".".join(parts) or "<root>"


def _reject_constant(_value: str) -> NoReturn:
    raise ValueError("Non-JSON constant")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


class CommentaryValidation:
    """One invocation, one diagnostic UUID; stop at the first failing stage."""

    def __init__(self, workflow: Workflow, model: type[BaseModel]):
        self.workflow = workflow
        self.model = model
        self.diagnostic_id = str(uuid4())
        self.started = perf_counter()
        self.response_length = 0

    def fail(self, stage: Stage, message: str, issues: list[tuple[tuple, str]]) -> NoReturn:
        errors = [{
            "path": _safe_path(self.model, loc, extra=kind == "extra_forbidden"),
            "type": kind if kind in _ERROR_TYPES else "validation_error",
        } for loc, kind in issues[:20]]
        event = {
            "event": "commentary_validation_failed", "diagnostic_id": self.diagnostic_id,
            "workflow": self.workflow, "stage": stage,
            "duration_ms": round((perf_counter() - self.started) * 1000, 3),
            "response_length": self.response_length, "error_count": len(issues),
            "errors": errors,
        }
        # Deliberately omit the provider model name: even bounded config can be a secret.
        logger = logging.getLogger("backend.loan_review" if self.workflow == "selected" else "backend.commentary")
        logger.warning(json.dumps(event, separators=(",", ":")))
        raise LLMError(f"{message} Diagnostic ID: {self.diagnostic_id}.") from None

    def parse(self, text: str, model: type[Output]) -> Output:
        self.response_length = len(text)
        candidate = text.strip()
        fence = re.fullmatch(r"```json[ \t]*\r?\n(.*?)\r?\n```", candidate, flags=re.DOTALL)
        if fence:
            candidate = fence.group(1)
        try:
            data = json.loads(candidate, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            self.fail("schema", _SCHEMA_MESSAGES[self.workflow], [((), "json_invalid")])
        # Literal[True] accepts 1 in Pydantic; require an actual JSON boolean as well.
        if isinstance(data, dict) and "requires_human_approval" in data and type(data["requires_human_approval"]) is not bool:
            self.fail("schema", _SCHEMA_MESSAGES[self.workflow], [(("requires_human_approval",), "bool_type")])
        try:
            return model.model_validate(data, strict=True)
        except ValidationError as exc:
            # Never use str(exc), msg, input, ctx, URLs, or unknown field names.
            issues = [(error["loc"], error["type"]) for error in
                      exc.errors(include_url=False, include_context=False, include_input=False)]
            self.fail("schema", _SCHEMA_MESSAGES[self.workflow], issues)