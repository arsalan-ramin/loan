import json
import logging

from fastapi.testclient import TestClient

from backend.main import create_app


def test_request_logs_are_persistent_bounded_and_content_free(tmp_path):
    root = tmp_path / "data"
    with TestClient(create_app(root)) as client:
        response = client.get("/api/health?secret=PRIVATE_QUERY_SENTINEL")
        assert response.status_code == 200
        assert response.headers["X-Request-ID"]
        handlers = [handler for handler in logging.getLogger("backend").handlers
                    if hasattr(handler, "maxBytes")]
        assert handlers[0].maxBytes == 5 * 1024 * 1024
        assert handlers[0].backupCount == 3
        log = (root / "logs" / "application.log").read_text(encoding="utf-8")
        assert "PRIVATE_QUERY_SENTINEL" not in log
        event = json.loads(next(line[line.index('{'):] for line in log.splitlines()
                                if '"event": "http_request_completed"' in line))
        assert event["route"] == "/api/health"
        assert event["status"] == 200
        assert event["request_id"] == response.headers["X-Request-ID"]