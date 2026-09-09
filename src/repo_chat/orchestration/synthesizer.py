"""Optional evidence-grounded LLM answer synthesis."""

import json
import re
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from repo_chat.contracts.evidence import Evidence
from repo_chat.contracts.orchestration import SynthesizedResponse


class AnswerSynthesizer(Protocol):
    """Boundary for an optional natural-language answer generator."""

    async def synthesize(self, query: str, draft: SynthesizedResponse) -> str: ...


class GroundedAnswer(BaseModel):
    """Validated model output before evidence markers are resolved."""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1)
    used_evidence_ids: list[int] = Field(min_length=1)


class CompatibleResponsesAnswerSynthesizer:
    """Use an OpenAI-compatible Responses endpoint for an evidence-only rewrite."""

    _citation_pattern = re.compile(r"\[E(\d+)\]")
    _numeric_citation_pattern = re.compile(r"(?<![A-Za-z])\[(\d+)\]")
    _evidence_like_pattern = re.compile(r"\[E[^\]]*\]")

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 20.0,
        maximum_input_characters: int = 20_000,
        maximum_output_tokens: int = 1_200,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._maximum_input_characters = maximum_input_characters
        self._maximum_output_tokens = maximum_output_tokens
        self._client = client

    async def synthesize(self, query: str, draft: SynthesizedResponse) -> str:
        """Generate and validate a readable answer with resolvable evidence markers."""
        if not draft.evidence:
            raise ValueError("LLM synthesis requires repository evidence")
        evidence_text = self._render_evidence(draft.evidence)
        dynamic_input = (
            f"Question:\n{query}\n\n"
            f"Deterministic draft:\n{draft.answer}\n\n"
            f"Repository evidence:\n{evidence_text}"
        )[: self._maximum_input_characters]
        payload = {
            "model": self._model,
            "store": False,
            "instructions": (
                "Answer the repository question using only the supplied deterministic draft "
                "and repository evidence. Do not add facts, paths, symbols, behavior, or "
                "citations that are absent from that material. Cite each factual claim with "
                "one or more markers such as [E1]. If evidence is insufficient, say so. "
                "Keep the answer concise and technically precise."
            ),
            "input": dynamic_input,
            "max_output_tokens": self._maximum_output_tokens,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "grounded_repository_answer",
                    "strict": True,
                    "schema": GroundedAnswer.model_json_schema(),
                }
            },
        }
        raw = await self._post(payload)
        grounded = self._parse_response(raw)
        return self._resolve_citations(grounded, draft.evidence)

    async def _post(self, payload: dict[str, object]) -> dict[str, object]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if self._client is not None:
            response = await self._client.post(
                f"{self._base_url}/responses", json=payload, headers=headers
            )
        else:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    f"{self._base_url}/responses", json=payload, headers=headers
                )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("OpenAI response was not an object")
        return result

    @classmethod
    def _parse_response(cls, response: dict[str, object]) -> GroundedAnswer:
        if response.get("status") != "completed":
            raise ValueError("OpenAI response did not complete")
        output = response.get("output")
        if not isinstance(output, list):
            raise ValueError("OpenAI response did not contain output")
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "refusal":
                    raise ValueError("OpenAI response was refused")
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    try:
                        return GroundedAnswer.model_validate(json.loads(part["text"]))
                    except (json.JSONDecodeError, ValidationError) as error:
                        raise ValueError("OpenAI structured output was invalid") from error
        raise ValueError("OpenAI response did not contain answer text")

    @classmethod
    def _resolve_citations(cls, result: GroundedAnswer, evidence: list[Evidence]) -> str:
        used = set(result.used_evidence_ids)
        markers = {
            int(value)
            for pattern in (cls._citation_pattern, cls._numeric_citation_pattern)
            for value in pattern.findall(result.answer)
        }
        evidence_like_markers = cls._evidence_like_pattern.findall(result.answer)
        valid = set(range(1, len(evidence) + 1))
        if not used or not used <= valid or not markers <= used:
            raise ValueError("LLM answer contained invalid evidence citations")
        if any(cls._citation_pattern.fullmatch(marker) is None for marker in evidence_like_markers):
            raise ValueError("LLM answer contained malformed evidence citations")
        answer = result.answer
        for evidence_id in sorted(markers, reverse=True):
            label = cls._citation_label(evidence[evidence_id - 1])
            answer = answer.replace(f"[E{evidence_id}]", label)
            answer = answer.replace(f"[{evidence_id}]", label)
        if not markers:
            labels = [cls._citation_label(evidence[index - 1]) for index in sorted(used)]
            answer = f"{answer.rstrip()}\n\nSources: {' '.join(labels)}"
        return answer

    @staticmethod
    def _render_evidence(evidence: list[Evidence]) -> str:
        sections = []
        for index, item in enumerate(evidence, start=1):
            location = CompatibleResponsesAnswerSynthesizer._citation_label(item)
            excerpt = (item.excerpt or "")[:1_500]
            graph_path = " -> ".join(item.graph_path[:12])
            sections.append(
                f"E{index}\nkind: {item.kind.value}\nlocation: {location}\n"
                f"entity_id: {item.entity_id or ''}\ngraph_path: {graph_path}\n"
                f"excerpt:\n{excerpt}"
            )
        return "\n\n".join(sections)

    @staticmethod
    def _citation_label(evidence: Evidence) -> str:
        if evidence.location is not None:
            location = evidence.location
            return f"[{location.file_path}:{location.start_line}-{location.end_line}]"
        if evidence.graph_path:
            return f"[graph: {' -> '.join(evidence.graph_path)}]"
        return f"[graph entity: {evidence.entity_id or 'unknown'}]"
