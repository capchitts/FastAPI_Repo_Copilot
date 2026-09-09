"""Safe, deterministic Python AST extraction for the Indexer agent."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from repo_chat.contracts.indexing import (
    CodeEntity,
    CodeRelationship,
    EntityKind,
    ParsedFile,
    ParseIssue,
    RelationshipKind,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _expression(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    return ast.unparse(node)


def module_name_from_path(file_path: str) -> str:
    """Convert a normalized Python file path to its import-style module name."""
    path = PurePosixPath(file_path)
    without_suffix = path.with_suffix("")
    parts = list(without_suffix.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


@dataclass(frozen=True, slots=True)
class _Scope:
    entity_id: str
    qualified_name: str
    kind: EntityKind


class PythonAstParser(ast.NodeVisitor):
    """Extract code entities and conservative static relationships from Python."""

    def __init__(
        self,
        *,
        repository_id: str,
        revision: str,
        file_path: str,
        source: str,
    ) -> None:
        self.repository_id = repository_id
        self.revision = revision
        self.file_path = file_path
        self.source = source
        self.content_hash = _digest(source)
        self.module_name = module_name_from_path(file_path)
        self.entities: list[CodeEntity] = []
        self.relationships: list[CodeRelationship] = []
        self._scopes: list[_Scope] = []

    def parse(self) -> ParsedFile:
        """Parse configured source and return normalized entities and edges."""
        try:
            tree = ast.parse(self.source, filename=self.file_path, type_comments=True)
        except SyntaxError as error:
            return ParsedFile(
                repository_id=self.repository_id,
                revision=self.revision,
                file_path=self.file_path,
                module_name=self.module_name,
                content_hash=self.content_hash,
                issues=[
                    ParseIssue(
                        file_path=self.file_path,
                        message=error.msg,
                        line=error.lineno,
                        fatal=True,
                    )
                ],
            )

        final_line = max(1, len(self.source.splitlines()))
        file_entity = self._entity(
            EntityKind.FILE,
            self.file_path,
            self.file_path,
            1,
            final_line,
        )
        module_entity = self._entity(
            EntityKind.MODULE,
            self.module_name.rsplit(".", 1)[-1] if self.module_name else self.file_path,
            self.module_name,
            1,
            final_line,
        )
        self._relationship(
            RelationshipKind.CONTAINS,
            file_entity.id,
            module_entity.id,
            module_entity.qualified_name,
            1,
            resolved=True,
        )
        self._scopes.append(
            _Scope(module_entity.id, module_entity.qualified_name, EntityKind.MODULE)
        )
        self._add_docstring(tree, module_entity)
        self.visit(tree)
        self._scopes.pop()

        return ParsedFile(
            repository_id=self.repository_id,
            revision=self.revision,
            file_path=self.file_path,
            module_name=self.module_name,
            content_hash=self.content_hash,
            entities=self.entities,
            relationships=self.relationships,
        )

    @property
    def _scope(self) -> _Scope:
        return self._scopes[-1]

    def _stable_id(self, kind: EntityKind, qualified_name: str) -> str:
        identity = "|".join((self.repository_id, self.file_path, kind.value, qualified_name))
        return f"{kind.value}:{_digest(identity)}"

    def _symbol_id(self, kind: EntityKind, qualified_name: str) -> str:
        identity = "|".join((self.repository_id, kind.value, qualified_name))
        return f"symbol:{kind.value}:{_digest(identity)}"

    def _entity(
        self,
        kind: EntityKind,
        name: str,
        qualified_name: str,
        start_line: int,
        end_line: int,
        metadata: dict[str, Any] | None = None,
    ) -> CodeEntity:
        entity = CodeEntity(
            id=self._stable_id(kind, qualified_name),
            kind=kind,
            name=name,
            qualified_name=qualified_name,
            file_path=self.file_path,
            start_line=start_line,
            end_line=end_line,
            content_hash=self.content_hash,
            metadata=metadata or {},
        )
        self.entities.append(entity)
        return entity

    def _relationship(
        self,
        kind: RelationshipKind,
        source_id: str,
        target_id: str,
        target_qualified_name: str,
        line: int,
        *,
        resolved: bool,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.relationships.append(
            CodeRelationship(
                kind=kind,
                source_id=source_id,
                target_id=target_id,
                target_qualified_name=target_qualified_name,
                resolved=resolved,
                file_path=self.file_path,
                line=line,
                metadata=metadata or {},
            )
        )

    def _qualified_name(self, name: str) -> str:
        prefix = self._scope.qualified_name
        return f"{prefix}.{name}" if prefix else name

    def _contain(self, entity: CodeEntity) -> None:
        self._relationship(
            RelationshipKind.CONTAINS,
            self._scope.entity_id,
            entity.id,
            entity.qualified_name,
            entity.start_line,
            resolved=True,
        )

    def _add_docstring(
        self,
        node: ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        owner: CodeEntity,
    ) -> None:
        value = ast.get_docstring(node, clean=False)
        if value is None or not node.body:
            return
        expression_node = node.body[0]
        doc_qualified_name = f"{owner.qualified_name}.__doc__"
        doc = self._entity(
            EntityKind.DOCSTRING,
            "__doc__",
            doc_qualified_name,
            expression_node.lineno,
            expression_node.end_lineno or expression_node.lineno,
            {"text": value, "summary": value.strip().splitlines()[0]},
        )
        self._relationship(
            RelationshipKind.DOCUMENTED_BY,
            owner.id,
            doc.id,
            doc.qualified_name,
            expression_node.lineno,
            resolved=True,
        )

    def _add_decorators(self, owner: CodeEntity, decorators: list[ast.expr]) -> None:
        for position, decorator_node in enumerate(decorators):
            expression = ast.unparse(decorator_node)
            qualified_name = f"{owner.qualified_name}.__decorator__.{position}"
            decorator = self._entity(
                EntityKind.DECORATOR,
                expression,
                qualified_name,
                decorator_node.lineno,
                decorator_node.end_lineno or decorator_node.lineno,
                {"expression": expression},
            )
            self._relationship(
                RelationshipKind.DECORATED_BY,
                owner.id,
                decorator.id,
                decorator.qualified_name,
                decorator_node.lineno,
                resolved=True,
            )

    def _add_parameters(
        self,
        owner: CodeEntity,
        arguments: ast.arguments,
    ) -> None:
        positional = [*arguments.posonlyargs, *arguments.args]
        defaults: list[ast.expr | None] = [None] * (
            len(positional) - len(arguments.defaults)
        ) + list(arguments.defaults)
        all_parameters: list[tuple[ast.arg, str, ast.expr | None]] = [
            (parameter, "positional", default)
            for parameter, default in zip(positional, defaults, strict=True)
        ]
        if arguments.vararg is not None:
            all_parameters.append((arguments.vararg, "var_positional", None))
        all_parameters.extend(
            (parameter, "keyword_only", default)
            for parameter, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True)
        )
        if arguments.kwarg is not None:
            all_parameters.append((arguments.kwarg, "var_keyword", None))

        for position, (parameter_node, parameter_kind, default) in enumerate(all_parameters):
            qualified_name = f"{owner.qualified_name}.{parameter_node.arg}"
            parameter = self._entity(
                EntityKind.PARAMETER,
                parameter_node.arg,
                qualified_name,
                parameter_node.lineno,
                parameter_node.end_lineno or parameter_node.lineno,
                {
                    "position": position,
                    "parameter_kind": parameter_kind,
                    "annotation": _expression(parameter_node.annotation),
                    "default": _expression(default),
                },
            )
            self._relationship(
                RelationshipKind.HAS_PARAMETER,
                owner.id,
                parameter.id,
                parameter.qualified_name,
                parameter.start_line,
                resolved=True,
            )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified_name = self._qualified_name(node.name)
        entity = self._entity(
            EntityKind.CLASS,
            node.name,
            qualified_name,
            node.lineno,
            node.end_lineno or node.lineno,
        )
        self._contain(entity)
        for base in node.bases:
            base_name = ast.unparse(base)
            self._relationship(
                RelationshipKind.INHERITS_FROM,
                entity.id,
                self._symbol_id(EntityKind.CLASS, base_name),
                base_name,
                base.lineno,
                resolved=False,
            )
        self._add_decorators(entity, node.decorator_list)
        self._add_docstring(node, entity)
        self._scopes.append(_Scope(entity.id, entity.qualified_name, entity.kind))
        for statement in node.body:
            self.visit(statement)
        self._scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, is_async=True)

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        is_async: bool,
    ) -> None:
        kind = EntityKind.METHOD if self._scope.kind is EntityKind.CLASS else EntityKind.FUNCTION
        qualified_name = self._qualified_name(node.name)
        entity = self._entity(
            kind,
            node.name,
            qualified_name,
            node.lineno,
            node.end_lineno or node.lineno,
            {
                "is_async": is_async,
                "returns": _expression(node.returns),
            },
        )
        self._contain(entity)
        self._add_decorators(entity, node.decorator_list)
        self._add_parameters(entity, node.args)
        self._add_docstring(node, entity)
        self._scopes.append(_Scope(entity.id, entity.qualified_name, entity.kind))
        for statement in node.body:
            self.visit(statement)
        self._scopes.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for position, alias in enumerate(node.names):
            self._add_import(node, alias.name, alias.asname, 0, position)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        prefix = "." * node.level
        for position, alias in enumerate(node.names):
            imported_name = f"{prefix}{module}"
            if alias.name != "*":
                separator = "" if imported_name.endswith(".") else "."
                imported_name = (
                    f"{imported_name}{separator}{alias.name}"
                    if imported_name
                    else alias.name
                )
            self._add_import(node, imported_name, alias.asname, node.level, position)

    def _add_import(
        self,
        node: ast.Import | ast.ImportFrom,
        imported_name: str,
        alias: str | None,
        level: int,
        position: int,
    ) -> None:
        qualified_name = f"{self._scope.qualified_name}.__import__.{node.lineno}.{position}"
        entity = self._entity(
            EntityKind.IMPORT,
            imported_name,
            qualified_name,
            node.lineno,
            node.end_lineno or node.lineno,
            {"imported_name": imported_name, "alias": alias, "level": level},
        )
        self._relationship(
            RelationshipKind.IMPORTS,
            self._scope.entity_id,
            entity.id,
            entity.qualified_name,
            node.lineno,
            resolved=True,
        )

    def visit_Call(self, node: ast.Call) -> None:
        if self._scope.kind in {EntityKind.FUNCTION, EntityKind.METHOD}:
            target_name = ast.unparse(node.func)
            self._relationship(
                RelationshipKind.CALLS,
                self._scope.entity_id,
                self._symbol_id(EntityKind.FUNCTION, target_name),
                target_name,
                node.lineno,
                resolved=False,
            )
        self.generic_visit(node)


def parse_python_source(
    source: str,
    *,
    repository_id: str,
    revision: str,
    file_path: str,
) -> ParsedFile:
    """Parse Python source text without importing or executing it."""
    return PythonAstParser(
        repository_id=repository_id,
        revision=revision,
        file_path=file_path,
        source=source,
    ).parse()


def parse_python_file(
    path: Path,
    *,
    repository_root: Path,
    repository_id: str,
    revision: str,
) -> ParsedFile:
    """Read and parse a UTF-8 Python file contained by a repository root."""
    root = repository_root.resolve()
    resolved_path = path.resolve()
    try:
        relative_path = resolved_path.relative_to(root)
    except ValueError as error:
        raise ValueError("source path must be contained by repository_root") from error
    source = resolved_path.read_text(encoding="utf-8")
    return parse_python_source(
        source,
        repository_id=repository_id,
        revision=revision,
        file_path=relative_path.as_posix(),
    )
