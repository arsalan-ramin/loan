from io import BytesIO

import pytest
from docx import Document
from fastapi.testclient import TestClient
from openpyxl import Workbook
from pptx import Presentation
from pypdf import PdfWriter

from .extraction import extract
from .main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("MARKETING_OLLAMA_MODEL", raising=False)
    with TestClient(create_app(tmp_path / "workspace.db")) as client:
        assert client.get("/api/workspace").json() == {
            "documents": [], "messages": [], "answer_mode": "excerpts",
        }
        yield client


def test_upload_context_scope_and_removal(client):
    files = [
        ("files", ("aurora.txt", b"Aurora's target audience is fitness subscribers.\nThe budget is $42,000.")),
        ("files", ("boreal.txt", b"Boreal's target audience is new parents.\nThe budget is $85,000.")),
        ("files", ("bad.png", b"no")),
    ]
    result = client.post("/api/documents", files=files)
    assert result.status_code == 200
    data = result.json()
    assert data["added"] == 2
    assert data["errors"][0]["name"] == "bad.png"
    docs = {doc["name"]: doc for doc in data["documents"]}
    assert docs["aurora.txt"]["metadata"]["passages"] == 1
    assert len(docs["aurora.txt"]["metadata"]["sha256"]) == 64
    assert client.post("/api/documents", files=files[:1]).json()["added"] == 0

    def ask(question, ids=()):
        reply = client.post("/api/chat", json={"question": question, "document_ids": list(ids)})
        assert reply.status_code == 200, reply.text
        return reply.json()["messages"][-1]

    audience = ask("Who is Aurora's target audience?")
    assert "fitness subscribers" in audience["text"]
    assert audience["sources"][0]["name"] == "aurora.txt"
    follow_up = ask("What is its budget?")
    assert "$42,000" in follow_up["text"] and "$85,000" not in follow_up["text"]
    filtered = ask("What is the budget?", [docs["boreal.txt"]["id"]])
    assert "$85,000" in filtered["text"] and "$42,000" not in filtered["text"]
    assert ask("intergalactic llamas")["sources"] == []
    assert len(client.get("/api/workspace").json()["messages"]) == 8
    assert client.delete("/api/documents/" + docs["aurora.txt"]["id"]).status_code == 204
    assert client.get("/api/workspace").json()["messages"] == []
    assert ask("Aurora")["sources"] == []


def test_workspace_isolation_and_cross_origin_rejection(client):
    docs = client.post("/api/documents", files={"files": ("private.txt", b"Private campaign")}).json()["documents"]
    doc_id = docs[0]["id"]
    with TestClient(client.app) as other:
        assert other.get("/api/workspace").json()["documents"] == []
        assert other.delete("/api/documents/" + doc_id).status_code == 404
    assert client.delete("/api/chat", headers={"Origin": "https://example.test"}).status_code == 403
    assert client.delete("/api/chat", headers={"Origin": "http://127.0.0.1:5174"}).status_code == 204
    assert client.delete("/api/chat").status_code == 204


@pytest.mark.parametrize("filename,raw,location", [
    ("campaign.md", b"# Audience: retail customers", "Document text"),
    ("metrics.csv", b"Channel,Clicks\nEmail,100\n", "CSV record 2"),
    ("brief.json", b'{"audience":"retail customers"}', "Document text"),
])
def test_text_formats(filename, raw, location):
    metadata, passages = extract(filename, raw)
    assert metadata["characters"] > 0
    assert passages[0][0] == location


def test_office_and_pdf_extraction():
    word = Document()
    word.core_properties.title = "Marketing brief"
    word.add_paragraph("Target audience: students.")
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[0])
    slide.shapes.title.text = "Email campaign"
    book = Workbook()
    book.active.append(["Channel", "Clicks"])
    book.active.append(["Email", 100])
    for name, file, value in [
        ("brief.docx", word, "Target audience"), ("campaign.pptx", deck, "Email campaign"),
        ("results.xlsx", book, "Clicks: 100"),
    ]:
        buffer = BytesIO()
        file.save(buffer)
        metadata, passages = extract(name, buffer.getvalue())
        assert metadata["passages"] == 1
        assert value in passages[0][1]

    pdf = PdfWriter()
    pdf.add_blank_page(100, 100)
    buffer = BytesIO()
    pdf.write(buffer)
    with pytest.raises(ValueError, match="No readable text"):
        extract("scanned.pdf", buffer.getvalue())


def test_model_mode_passes_context_and_checks_citations(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKETING_OLLAMA_MODEL", "test-model")

    def model(name, question, history, sources):
        assert name == "test-model"
        assert sources[0]["name"] == "brief.txt"
        if history:
            assert history[0]["text"] == "Who is the audience?"
            return "Invalid source [9]"
        return "The audience is students [1]."

    monkeypatch.setattr("marketing_backend.main.ollama_answer", model)
    with TestClient(create_app(tmp_path / "model.db")) as client:
        assert client.get("/api/workspace").json()["answer_mode"] == "local-model"
        assert client.post("/api/documents", files={"files": ("brief.txt", b"The audience is students.")}).status_code == 200
        response = client.post("/api/chat", json={"question": "Who is the audience?"})
        assert response.json()["messages"][1]["text"] == "The audience is students [1]."
        assert client.post("/api/chat", json={"question": "And the audience?"}).status_code == 502
        assert len(client.get("/api/workspace").json()["messages"]) == 2
