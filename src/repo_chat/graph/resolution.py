"""Conservative static symbol resolution before graph persistence."""

from collections import defaultdict
from pathlib import PurePosixPath

from repo_chat.contracts.indexing import (
    CodeEntity,
    CodeRelationship,
    EntityKind,
    ParsedFile,
    RelationshipKind,
)


def _candidate_names(reference: str, module_name: str, source: CodeEntity) -> tuple[str, ...]:
    candidates = [reference]
    if "." not in reference:
        if source.kind is EntityKind.METHOD:
            class_name = source.qualified_name.rsplit(".", 1)[0]
            candidates.append(f"{class_name}.{reference}")
        candidates.append(f"{module_name}.{reference}")
    return tuple(dict.fromkeys(candidates))


def resolve_local_relationships(parsed_file: ParsedFile) -> ParsedFile:
    """Resolve unambiguous call and inheritance targets defined in one file."""
    by_id = {entity.id: entity for entity in parsed_file.entities}
    by_qualified_name: dict[str, list[CodeEntity]] = defaultdict(list)
    by_simple_name: dict[str, list[CodeEntity]] = defaultdict(list)
    for entity in parsed_file.entities:
        by_qualified_name[entity.qualified_name].append(entity)
        by_simple_name[entity.name].append(entity)

    relationships: list[CodeRelationship] = []
    for relationship in parsed_file.relationships:
        if relationship.resolved or relationship.kind not in {
            RelationshipKind.CALLS,
            RelationshipKind.INHERITS_FROM,
        }:
            relationships.append(relationship)
            continue

        source = by_id.get(relationship.source_id)
        if source is None:
            relationships.append(relationship)
            continue

        candidates: list[CodeEntity] = []
        for name in _candidate_names(
            relationship.target_qualified_name,
            parsed_file.module_name,
            source,
        ):
            candidates.extend(by_qualified_name.get(name, []))
        if not candidates and "." not in relationship.target_qualified_name:
            candidates = by_simple_name.get(relationship.target_qualified_name, [])

        expected_kinds = (
            {EntityKind.CLASS}
            if relationship.kind is RelationshipKind.INHERITS_FROM
            else {EntityKind.FUNCTION, EntityKind.METHOD}
        )
        unique_candidates = {
            candidate.id: candidate for candidate in candidates if candidate.kind in expected_kinds
        }
        if len(unique_candidates) == 1:
            target = next(iter(unique_candidates.values()))
            relationship = relationship.model_copy(
                update={
                    "target_id": target.id,
                    "target_qualified_name": target.qualified_name,
                    "resolved": True,
                }
            )
        relationships.append(relationship)

    return parsed_file.model_copy(update={"relationships": relationships})


def resolve_repository_relationships(parsed_files: list[ParsedFile]) -> list[ParsedFile]:
    """Resolve unambiguous imports, aliases, inheritance, and calls across one revision."""
    locally_resolved = [resolve_local_relationships(parsed) for parsed in parsed_files]
    all_entities = [entity for parsed in locally_resolved for entity in parsed.entities]
    by_qualified: dict[str, list[CodeEntity]] = defaultdict(list)
    by_simple: dict[str, list[CodeEntity]] = defaultdict(list)
    for entity in all_entities:
        by_qualified[entity.qualified_name].append(entity)
        by_simple[entity.name].append(entity)

    output: list[ParsedFile] = []
    for parsed in locally_resolved:
        by_id = {entity.id: entity for entity in parsed.entities}
        aliases = _import_aliases(parsed)
        relationships: list[CodeRelationship] = []
        for relationship in parsed.relationships:
            if relationship.kind is RelationshipKind.IMPORTS:
                import_entity = by_id.get(relationship.target_id)
                target = _resolve_import_entity(import_entity, by_qualified)
                if target is not None and import_entity is not None:
                    relationships.append(relationship)
                    relationship = _resolved_relationship(
                        relationship.model_copy(
                            update={"kind": RelationshipKind.DEPENDS_ON}
                        ),
                        target,
                        confidence=1.0,
                        strategy="cross_file_import",
                        extra={"import_entity_id": import_entity.id},
                    )
            elif not relationship.resolved and relationship.kind in {
                RelationshipKind.CALLS,
                RelationshipKind.INHERITS_FROM,
            }:
                source = by_id.get(relationship.source_id)
                if source is not None:
                    target, confidence, strategy = _repository_target(
                        relationship,
                        source,
                        parsed.module_name,
                        aliases,
                        by_qualified,
                        by_simple,
                    )
                    if target is not None:
                        relationship = _resolved_relationship(
                            relationship,
                            target,
                            confidence=confidence,
                            strategy=strategy,
                        )
            relationships.append(relationship)
        output.append(parsed.model_copy(update={"relationships": relationships}))
    return output


def _import_aliases(parsed: ParsedFile) -> dict[str, str]:
    aliases: dict[str, str] = {}
    package = (
        parsed.module_name
        if PurePosixPath(parsed.file_path).name == "__init__.py"
        else parsed.module_name.rpartition(".")[0]
    )
    for entity in parsed.entities:
        if entity.kind is not EntityKind.IMPORT:
            continue
        imported = str(entity.metadata.get("imported_name", entity.name))
        absolute = _absolute_import(imported, package)
        alias = entity.metadata.get("alias")
        binding = str(alias) if alias else imported.lstrip(".").rsplit(".", 1)[-1]
        if entity.metadata.get("level", 0) == 0 and "." in imported and alias is None:
            binding = imported.split(".", 1)[0]
            absolute = binding
        aliases[binding] = absolute
    return aliases


def _absolute_import(imported: str, package: str) -> str:
    level = len(imported) - len(imported.lstrip("."))
    if level == 0:
        return imported
    remainder = imported[level:]
    parts = package.split(".") if package else []
    retained = parts[: max(0, len(parts) - level + 1)]
    return ".".join([*retained, *([remainder] if remainder else [])])


def _resolve_import_entity(
    entity: CodeEntity | None, by_qualified: dict[str, list[CodeEntity]]
) -> CodeEntity | None:
    if entity is None:
        return None
    imported = str(entity.metadata.get("imported_name", ""))
    module_name = entity.qualified_name.split(".__import__.", 1)[0]
    package = (
        module_name
        if PurePosixPath(entity.file_path).name == "__init__.py"
        else module_name.rpartition(".")[0]
    )
    absolute = _absolute_import(imported, package)
    candidates = by_qualified.get(absolute, [])
    return candidates[0] if len(candidates) == 1 else None


def _repository_target(
    relationship: CodeRelationship,
    source: CodeEntity,
    module_name: str,
    aliases: dict[str, str],
    by_qualified: dict[str, list[CodeEntity]],
    by_simple: dict[str, list[CodeEntity]],
) -> tuple[CodeEntity | None, float, str]:
    reference = relationship.target_qualified_name
    expected = (
        {EntityKind.CLASS}
        if relationship.kind is RelationshipKind.INHERITS_FROM
        else {EntityKind.FUNCTION, EntityKind.METHOD, EntityKind.CLASS}
    )
    candidates: list[tuple[str, float, str]] = []
    first, separator, remainder = reference.partition(".")
    if first in {"self", "cls"} and separator and source.kind is EntityKind.METHOD:
        class_name = source.qualified_name.rsplit(".", 1)[0]
        candidates.append((f"{class_name}.{remainder}", 0.95, "instance_method"))
    if first in aliases:
        target_name = aliases[first] + (f".{remainder}" if separator else "")
        candidates.append((target_name, 1.0, "import_alias"))
    candidates.extend(
        [
            (reference, 0.95, "qualified_name"),
            (f"{module_name}.{reference}", 0.9, "module_scope"),
        ]
    )
    for qualified_name, confidence, strategy in candidates:
        matches = [item for item in by_qualified.get(qualified_name, []) if item.kind in expected]
        if len(matches) == 1:
            return matches[0], confidence, strategy
    if "." not in reference:
        matches = [item for item in by_simple.get(reference, []) if item.kind in expected]
        if len(matches) == 1:
            return matches[0], 0.75, "unique_repository_symbol"
    return None, 0.0, "unresolved"


def _resolved_relationship(
    relationship: CodeRelationship,
    target: CodeEntity,
    *,
    confidence: float,
    strategy: str,
    extra: dict[str, object] | None = None,
) -> CodeRelationship:
    return relationship.model_copy(
        update={
            "target_id": target.id,
            "target_qualified_name": target.qualified_name,
            "resolved": True,
            "metadata": {
                **relationship.metadata,
                "resolution_scope": "repository",
                "resolution_strategy": strategy,
                "resolution_confidence": confidence,
                **(extra or {}),
            },
        }
    )
