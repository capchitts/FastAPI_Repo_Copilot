"""Deterministic source analysis behind the Code Analyst MCP server."""

import ast
from collections.abc import Iterable
from dataclasses import dataclass

from repo_chat.analysis.source_store import SourceStore
from repo_chat.contracts.code_analysis import (
    CodeSnippet,
    EntityAnalysis,
    ImplementationComparison,
    PatternAnalysis,
    PatternKind,
    PatternMatch,
)
from repo_chat.contracts.indexing import EntityKind
from repo_chat.exceptions.base import EntityNotFoundError


@dataclass(frozen=True, slots=True)
class _LocatedEntity:
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
    qualified_name: str


class CodeAnalysisService:
    """Analyze repository source without importing or executing it."""

    def __init__(
        self,
        source_store: SourceStore,
        *,
        maximum_entity_snippet_lines: int = 40,
    ) -> None:
        if not 5 <= maximum_entity_snippet_lines <= 200:
            raise ValueError("maximum_entity_snippet_lines must be between 5 and 200")
        self._source_store = source_store
        self._maximum_entity_snippet_lines = maximum_entity_snippet_lines

    def get_code_snippet(
        self,
        *,
        repository_id: str,
        revision: str,
        file_path: str,
        start_line: int,
        end_line: int,
        context_lines: int = 3,
    ) -> CodeSnippet:
        """Retrieve code with exact source provenance."""
        return self._source_store.read_snippet(
            repository_id=repository_id,
            revision=revision,
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            context_lines=context_lines,
        )

    def analyze_function(self, *, function_name: str, **location: str) -> EntityAnalysis:
        """Analyze a function or method selected by qualified or simple name."""
        return self._analyze_entity(
            entity_name=function_name, allowed=(ast.FunctionDef, ast.AsyncFunctionDef), **location
        )

    def analyze_class(self, *, class_name: str, **location: str) -> EntityAnalysis:
        """Analyze a class selected by qualified or simple name."""
        return self._analyze_entity(entity_name=class_name, allowed=(ast.ClassDef,), **location)

    def explain_implementation(self, *, entity_name: str, **location: str) -> EntityAnalysis:
        """Explain a source entity using deterministic structural facts."""
        return self._analyze_entity(
            entity_name=entity_name,
            allowed=(ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            **location,
        )

    def find_patterns(
        self, *, repository_id: str, revision: str, file_path: str
    ) -> PatternAnalysis:
        """Detect conservative statically observable patterns."""
        whole_file = self._source_store.read_file(repository_id, revision, file_path)
        tree = ast.parse(whole_file.code, filename=file_path)
        patterns: list[PatternMatch] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.bases:
                patterns.append(
                    PatternMatch(
                        kind=PatternKind.INHERITANCE,
                        entity_name=node.name,
                        rationale=(
                            f"Inherits from {', '.join(ast.unparse(base) for base in node.bases)}."
                        ),
                        line=node.lineno,
                    )
                )
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                patterns.extend(
                    PatternMatch(
                        kind=PatternKind.DECORATOR,
                        entity_name=node.name,
                        rationale=f"Uses decorator {ast.unparse(decorator)}.",
                        line=decorator.lineno,
                    )
                    for decorator in node.decorator_list
                )
            if isinstance(node, ast.AsyncFunctionDef):
                patterns.append(
                    PatternMatch(
                        kind=PatternKind.ASYNC_IO,
                        entity_name=node.name,
                        rationale="Declared with async def.",
                        line=node.lineno,
                    )
                )
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("Depends"):
                patterns.append(
                    PatternMatch(
                        kind=PatternKind.DEPENDENCY_INJECTION,
                        entity_name=ast.unparse(node.func),
                        rationale="Constructs a FastAPI dependency marker.",
                        line=node.lineno,
                    )
                )
        return PatternAnalysis(file_path=file_path, patterns=patterns, evidence=whole_file.evidence)

    def compare_implementations(
        self,
        *,
        repository_id: str,
        revision: str,
        left_file_path: str,
        left_entity_name: str,
        right_file_path: str,
        right_entity_name: str,
    ) -> ImplementationComparison:
        """Compare two entities using their structural analyses."""
        left = self.explain_implementation(
            repository_id=repository_id,
            revision=revision,
            file_path=left_file_path,
            entity_name=left_entity_name,
        )
        right = self.explain_implementation(
            repository_id=repository_id,
            revision=revision,
            file_path=right_file_path,
            entity_name=right_entity_name,
        )
        similarities: list[str] = []
        differences: list[str] = []
        if left.kind is right.kind:
            similarities.append(f"Both entities are {left.kind.value}s.")
        else:
            differences.append(f"Kinds differ: {left.kind.value} versus {right.kind.value}.")
        shared_decorators = sorted(set(left.decorators) & set(right.decorators))
        if shared_decorators:
            similarities.append(f"Shared decorators: {', '.join(shared_decorators)}.")
        if left.parameters == right.parameters:
            similarities.append("They expose the same named parameter interface.")
        else:
            differences.append("Their parameter lists differ.")
        if left.bases != right.bases:
            differences.append("Their base classes differ.")
        if left.methods != right.methods:
            differences.append("Their declared method sets differ.")
        left_only_calls = sorted(set(left.calls) - set(right.calls))
        right_only_calls = sorted(set(right.calls) - set(left.calls))
        if left_only_calls or right_only_calls:
            differences.append(
                "Their implementations construct or call different collaborators: "
                f"{left.qualified_name} uniquely uses "
                f"{', '.join(left_only_calls) or 'none'}, while {right.qualified_name} "
                f"uniquely uses {', '.join(right_only_calls) or 'none'}."
            )
        left_purpose = self._first_paragraph(left.docstring)
        right_purpose = self._first_paragraph(right.docstring)
        if left_purpose and right_purpose and left_purpose != right_purpose:
            differences.append(
                f"Their documented roles differ: {left.qualified_name} — "
                f"{left_purpose.rstrip('.')}; {right.qualified_name} — "
                f"{right_purpose.rstrip('.')}."
            )
        return ImplementationComparison(
            left=left,
            right=right,
            similarities=similarities,
            differences=differences,
            summary=(
                f"Compared {left.qualified_name} with {right.qualified_name}; "
                f"found {len(similarities)} "
                f"{'similarity' if len(similarities) == 1 else 'similarities'} and "
                f"{len(differences)} "
                f"{'difference' if len(differences) == 1 else 'differences'}."
            ),
        )

    @staticmethod
    def _first_paragraph(docstring: str | None) -> str:
        """Collapse the first docstring paragraph for bounded comparison output."""
        if not docstring:
            return ""
        return " ".join(docstring.strip().split("\n\n", 1)[0].split())

    def _analyze_entity(
        self,
        *,
        repository_id: str,
        revision: str,
        file_path: str,
        entity_name: str,
        allowed: tuple[
            type[ast.ClassDef] | type[ast.FunctionDef] | type[ast.AsyncFunctionDef], ...
        ],
    ) -> EntityAnalysis:
        whole_file = self._source_store.read_file(repository_id, revision, file_path)
        tree = ast.parse(whole_file.code, filename=file_path)
        located = self._locate(tree, entity_name, allowed)
        node = located.node
        snippet = self._source_store.read_snippet(
            repository_id=repository_id,
            revision=revision,
            file_path=file_path,
            start_line=node.lineno,
            end_line=min(
                node.end_lineno or node.lineno,
                node.lineno + self._maximum_entity_snippet_lines - 1,
            ),
        )
        decorators = [ast.unparse(item) for item in node.decorator_list]
        calls = sorted(
            {ast.unparse(call.func) for call in ast.walk(node) if isinstance(call, ast.Call)}
        )
        awaits = self._bounded_unparse(
            await_node.value for await_node in ast.walk(node) if isinstance(await_node, ast.Await)
        )
        returns = self._bounded_unparse(
            return_node.value
            for return_node in ast.walk(node)
            if isinstance(return_node, ast.Return) and return_node.value is not None
        )
        raises = self._bounded_unparse(
            raise_node.exc
            for raise_node in ast.walk(node)
            if isinstance(raise_node, ast.Raise) and raise_node.exc is not None
        )
        control_flow = self._control_flow(node)
        if isinstance(node, ast.ClassDef):
            kind = EntityKind.CLASS
            bases = [ast.unparse(base) for base in node.bases]
            signature = f"class {node.name}" + (f"({', '.join(bases)})" if bases else "")
            parameters: list[str] = []
            methods = [
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            base_label = "base class" if len(bases) == 1 else "base classes"
            explanation = (
                f"{located.qualified_name} is a class with {len(methods)} directly "
                f"declared methods and {len(bases)} {base_label}."
            )
        else:
            kind = EntityKind.METHOD if "." in located.qualified_name else EntityKind.FUNCTION
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            parameters = [arg.arg for arg in (*node.args.posonlyargs, *node.args.args)]
            if node.args.vararg:
                parameters.append(f"*{node.args.vararg.arg}")
            parameters.extend(arg.arg for arg in node.args.kwonlyargs)
            if node.args.kwarg:
                parameters.append(f"**{node.args.kwarg.arg}")
            signature = f"{prefix} {node.name}({', '.join(parameters)})"
            bases, methods = [], []
            callable_kind = "an asynchronous" if prefix == "async def" else "a synchronous"
            explanation = (
                f"{located.qualified_name} is {callable_kind} callable with "
                f"{len(parameters)} parameters and {len(calls)} distinct calls."
            )
        return EntityAnalysis(
            name=node.name,
            qualified_name=located.qualified_name,
            kind=kind,
            signature=signature,
            docstring=ast.get_docstring(node, clean=False),
            decorators=decorators,
            parameters=parameters,
            bases=bases,
            methods=methods,
            calls=calls,
            awaits=awaits,
            control_flow=control_flow,
            returns=returns,
            raises=raises,
            explanation=explanation,
            snippet=snippet,
        )

    @staticmethod
    def _bounded_unparse(nodes: Iterable[ast.AST], *, limit: int = 12) -> list[str]:
        """Render unique AST expressions without producing unbounded tool output."""
        rendered: list[str] = []
        for item in nodes:
            value = ast.unparse(item)
            if value not in rendered:
                rendered.append(value)
            if len(rendered) == limit:
                break
        return rendered

    @staticmethod
    def _control_flow(
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, *, limit: int = 16
    ) -> list[str]:
        """Describe statically observable decisions in source order."""
        observations: list[tuple[int, str]] = []
        for item in ast.walk(node):
            if isinstance(item, ast.If):
                observations.append(
                    (item.lineno, f"line {item.lineno}: if {ast.unparse(item.test)}")
                )
            elif isinstance(item, (ast.For, ast.AsyncFor)):
                prefix = "async for" if isinstance(item, ast.AsyncFor) else "for"
                observations.append(
                    (
                        item.lineno,
                        f"line {item.lineno}: {prefix} {ast.unparse(item.target)} in "
                        f"{ast.unparse(item.iter)}",
                    )
                )
            elif isinstance(item, ast.While):
                observations.append(
                    (item.lineno, f"line {item.lineno}: while {ast.unparse(item.test)}")
                )
            elif isinstance(item, ast.Try):
                handled = ", ".join(
                    ast.unparse(handler.type) if handler.type is not None else "all exceptions"
                    for handler in item.handlers
                )
                observations.append(
                    (item.lineno, f"line {item.lineno}: try/except handles {handled or 'none'}")
                )
            elif isinstance(item, (ast.With, ast.AsyncWith)):
                prefix = "async with" if isinstance(item, ast.AsyncWith) else "with"
                managers = ", ".join(ast.unparse(entry.context_expr) for entry in item.items)
                observations.append((item.lineno, f"line {item.lineno}: {prefix} {managers}"))
        return [description for _, description in sorted(observations)[:limit]]

    @staticmethod
    def _locate(
        tree: ast.Module,
        requested_name: str,
        allowed: tuple[
            type[ast.ClassDef] | type[ast.FunctionDef] | type[ast.AsyncFunctionDef], ...
        ],
    ) -> _LocatedEntity:
        found: list[_LocatedEntity] = []

        def visit(body: list[ast.stmt], prefix: str = "") -> None:
            for statement in body:
                if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    qualified = f"{prefix}.{statement.name}" if prefix else statement.name
                    if isinstance(statement, allowed) and requested_name in {
                        statement.name,
                        qualified,
                    }:
                        found.append(_LocatedEntity(statement, qualified))
                    visit(statement.body, qualified)

        visit(tree.body)
        if not found:
            raise EntityNotFoundError(
                "Source entity was not found", details={"entity_name": requested_name}
            )
        if len(found) > 1:
            raise EntityNotFoundError(
                "Source entity name is ambiguous; use its qualified name",
                details={"entity_name": requested_name},
            )
        return found[0]
