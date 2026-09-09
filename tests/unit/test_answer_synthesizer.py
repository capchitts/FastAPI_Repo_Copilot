import json

import httpx
import pytest

from repo_chat.contracts.evidence import Evidence, EvidenceKind, SourceLocation
from repo_chat.contracts.orchestration import SynthesizedResponse
from repo_chat.orchestration.synthesizer import CompatibleResponsesAnswerSynthesizer


def _draft() -> SynthesizedResponse:
    return SynthesizedResponse(
        answer="APIRouter is declared in routing.py.",
        evidence=[
            Evidence(
                kind=EvidenceKind.SOURCE,
                location=SourceLocation(
                    repository_id="fastapi",
                    revision="abc123",
                    file_path="fastapi/routing.py",
                    start_line=10,
                    end_line=20,
                ),
                excerpt="class APIRouter:",
            )
        ],
    )


def _response(answer: str, evidence_ids: list[int]) -> dict[str, object]:
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps({"answer": answer, "used_evidence_ids": evidence_ids}),
                    }
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_openai_synthesizer_uses_structured_outputs_and_resolves_citations() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json=_response("APIRouter groups routes [E1].", [1]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        synthesizer = CompatibleResponsesAnswerSynthesizer(
            api_key="test-key", model="test-model", client=client
        )
        answer = await synthesizer.synthesize("What is APIRouter?", _draft())

    assert answer == "APIRouter groups routes [fastapi/routing.py:10-20]."
    assert captured["store"] is False
    assert captured["text"] == {
        "format": {
            "type": "json_schema",
            "name": "grounded_repository_answer",
            "strict": True,
            "schema": captured["text"]["format"]["schema"],  # type: ignore[index]
        }
    }


@pytest.mark.asyncio
async def test_openai_synthesizer_rejects_invented_evidence_id() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_response("Invented claim [E2].", [2]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        synthesizer = CompatibleResponsesAnswerSynthesizer(
            api_key="test-key", model="test-model", client=client
        )
        with pytest.raises(ValueError, match="invalid evidence citations"):
            await synthesizer.synthesize("What is APIRouter?", _draft())


@pytest.mark.asyncio
async def test_openai_synthesizer_appends_validated_sources_when_markers_are_omitted() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_response("APIRouter groups routes.", [1]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        synthesizer = CompatibleResponsesAnswerSynthesizer(
            api_key="test-key", model="test-model", client=client
        )
        answer = await synthesizer.synthesize("What is APIRouter?", _draft())

    assert answer == ("APIRouter groups routes.\n\nSources: [fastapi/routing.py:10-20]")


@pytest.mark.asyncio
async def test_openai_synthesizer_resolves_provider_numeric_citation_style() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_response("APIRouter groups routes [1].", [1]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        synthesizer = CompatibleResponsesAnswerSynthesizer(
            api_key="test-key", model="test-model", client=client
        )
        answer = await synthesizer.synthesize("What is APIRouter?", _draft())

    assert answer == "APIRouter groups routes [fastapi/routing.py:10-20]."


@pytest.mark.asyncio
async def test_openai_synthesizer_rejects_malformed_evidence_marker() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_response("APIRouter groups routes [E1, E2].", [1]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        synthesizer = CompatibleResponsesAnswerSynthesizer(
            api_key="test-key", model="test-model", client=client
        )
        with pytest.raises(ValueError, match="malformed evidence citations"):
            await synthesizer.synthesize("What is APIRouter?", _draft())


@pytest.mark.asyncio
async def test_synthesizer_allows_declared_but_not_inline_cited_evidence() -> None:
    draft = _draft().model_copy(
        update={
            "evidence": [
                *_draft().evidence,
                Evidence(kind=EvidenceKind.GRAPH, entity_id="class-1"),
            ]
        }
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_response("Source claim [E1].", [1, 2]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        synthesizer = CompatibleResponsesAnswerSynthesizer(
            api_key="test-key", model="test-model", client=client
        )
        answer = await synthesizer.synthesize("What is APIRouter?", draft)

    assert answer == "Source claim [fastapi/routing.py:10-20]."


@pytest.mark.asyncio
async def test_openai_synthesizer_rejects_refusal() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": "Cannot answer"}],
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        synthesizer = CompatibleResponsesAnswerSynthesizer(
            api_key="test-key", model="test-model", client=client
        )
        with pytest.raises(ValueError, match="refused"):
            await synthesizer.synthesize("What is APIRouter?", _draft())
