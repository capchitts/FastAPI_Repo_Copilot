from pathlib import Path

import pytest

from repo_chat.analysis.service import CodeAnalysisService
from repo_chat.analysis.source_store import SourceStore
from repo_chat.contracts.code_analysis import PatternKind
from repo_chat.contracts.indexing import EntityKind
from repo_chat.exceptions.base import EntityNotFoundError

SOURCE = '''"""Analysis fixture."""

from fastapi import Depends

def traced(function):
    return function

class Base:
    pass

@traced
class Service(Base):
    """Example service."""

    async def execute(self, value: int = Depends(get_value)) -> int:
        """Execute the service."""
        return helper(value)

def helper(value: int) -> int:
    return value

def get_value() -> int:
    return 1
'''


def create_service(tmp_path: Path) -> CodeAnalysisService:
    repository = tmp_path / "repositories" / "fastapi"
    repository.mkdir(parents=True)
    (repository / "service.py").write_text(SOURCE)
    return CodeAnalysisService(SourceStore(tmp_path / "repositories"))


def test_analyzes_class_and_method(tmp_path: Path) -> None:
    service = create_service(tmp_path)

    class_analysis = service.analyze_class(
        repository_id="fastapi", revision="abc123", file_path="service.py", class_name="Service"
    )
    method_analysis = service.analyze_function(
        repository_id="fastapi",
        revision="abc123",
        file_path="service.py",
        function_name="Service.execute",
    )

    assert class_analysis.kind is EntityKind.CLASS
    assert class_analysis.bases == ["Base"]
    assert class_analysis.methods == ["execute"]
    assert class_analysis.decorators == ["traced"]
    assert class_analysis.docstring == "Example service."
    assert method_analysis.kind is EntityKind.METHOD
    assert method_analysis.signature == "async def execute(self, value)"
    assert method_analysis.calls == ["Depends", "helper"]
    assert method_analysis.snippet.code.startswith("    async def execute")


def test_detects_source_patterns(tmp_path: Path) -> None:
    result = create_service(tmp_path).find_patterns(
        repository_id="fastapi", revision="abc123", file_path="service.py"
    )
    kinds = {pattern.kind for pattern in result.patterns}

    assert PatternKind.INHERITANCE in kinds
    assert PatternKind.DECORATOR in kinds
    assert PatternKind.ASYNC_IO in kinds
    assert PatternKind.DEPENDENCY_INJECTION in kinds


def test_compares_implementations(tmp_path: Path) -> None:
    comparison = create_service(tmp_path).compare_implementations(
        repository_id="fastapi",
        revision="abc123",
        left_file_path="service.py",
        left_entity_name="helper",
        right_file_path="service.py",
        right_entity_name="get_value",
    )

    assert comparison.left.kind is EntityKind.FUNCTION
    assert comparison.right.kind is EntityKind.FUNCTION
    assert comparison.similarities == ["Both entities are functions."]
    assert "parameter lists differ" in comparison.differences[0]


def test_comparison_detects_different_collaborators_and_documented_roles(tmp_path: Path) -> None:
    repository = tmp_path / "repositories" / "fastapi"
    repository.mkdir(parents=True)
    (repository / "comparison.py").write_text(
        'def left(value):\n    """Build a path parameter."""\n    return make_path(value)\n\n'
        'def right(value):\n    """Build a query parameter."""\n    return make_query(value)\n'
    )
    service = CodeAnalysisService(SourceStore(tmp_path / "repositories"))

    comparison = service.compare_implementations(
        repository_id="fastapi",
        revision="abc123",
        left_file_path="comparison.py",
        left_entity_name="left",
        right_file_path="comparison.py",
        right_entity_name="right",
    )

    assert any("same named parameter interface" in item for item in comparison.similarities)
    assert any(
        "make_path" in difference and "make_query" in difference
        for difference in comparison.differences
    )
    assert any("documented roles differ" in difference for difference in comparison.differences)
    assert "2 similarities and 2 differences" in comparison.summary


def test_comparison_summary_uses_singular_grammar(tmp_path: Path) -> None:
    comparison = create_service(tmp_path).compare_implementations(
        repository_id="fastapi",
        revision="abc123",
        left_file_path="service.py",
        left_entity_name="helper",
        right_file_path="service.py",
        right_entity_name="get_value",
    )

    assert "1 similarity and 1 difference" in comparison.summary


def test_rejects_missing_and_ambiguous_entities(tmp_path: Path) -> None:
    service = create_service(tmp_path)
    with pytest.raises(EntityNotFoundError, match="not found"):
        service.analyze_class(
            repository_id="fastapi", revision="abc123", file_path="service.py", class_name="Missing"
        )

    repository = tmp_path / "repositories" / "fastapi"
    (repository / "ambiguous.py").write_text(
        "class A:\n    def same(self): pass\nclass B:\n    def same(self): pass\n"
    )
    with pytest.raises(EntityNotFoundError, match="ambiguous"):
        service.analyze_function(
            repository_id="fastapi",
            revision="abc123",
            file_path="ambiguous.py",
            function_name="same",
        )


def test_limits_large_entity_snippets_and_uses_singular_grammar(tmp_path: Path) -> None:
    repository = tmp_path / "repositories" / "fastapi"
    repository.mkdir(parents=True)
    body = "".join(f"    value_{index} = {index}\n" for index in range(100))
    (repository / "large.py").write_text(f"class Large(Base):\n{body}")
    service = CodeAnalysisService(
        SourceStore(tmp_path / "repositories"), maximum_entity_snippet_lines=20
    )

    result = service.analyze_class(
        repository_id="fastapi",
        revision="abc123",
        file_path="large.py",
        class_name="Large",
    )

    assert result.snippet.end_line - result.snippet.start_line + 1 == 20
    assert result.explanation.endswith("1 base class.")


def test_rejects_invalid_entity_snippet_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="between 5 and 200"):
        CodeAnalysisService(SourceStore(tmp_path), maximum_entity_snippet_lines=4)


def test_extracts_bounded_execution_behavior(tmp_path: Path) -> None:
    repository = tmp_path / "repositories" / "fastapi"
    repository.mkdir(parents=True)
    (repository / "execution.py").write_text(
        "async def dispatch(value):\n"
        "    if value is None:\n"
        "        raise ValueError('missing')\n"
        "    for item in value:\n"
        "        await send(item)\n"
        "    return build_response(value)\n"
    )
    service = CodeAnalysisService(SourceStore(tmp_path / "repositories"))

    result = service.analyze_function(
        repository_id="fastapi",
        revision="abc123",
        file_path="execution.py",
        function_name="dispatch",
    )

    assert result.awaits == ["send(item)"]
    assert result.control_flow == [
        "line 2: if value is None",
        "line 4: for item in value",
    ]
    assert result.returns == ["build_response(value)"]
    assert result.raises == ["ValueError('missing')"]
