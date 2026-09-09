from repo_chat.indexing.ast_parser import parse_python_source
from repo_chat.semantic.store import QdrantSemanticStore


class FakeQdrantClient:
    def __init__(self) -> None:
        self.deletions = []

    def delete(self, collection_name, *, points_selector, wait):  # type: ignore[no-untyped-def]
        self.deletions.append((collection_name, points_selector, wait))


def test_semantic_documents_are_symbol_scoped_and_revision_pinned() -> None:
    source = '''class TokenValidator:
    """Validate bearer tokens for protected endpoints."""
    def validate(self, token: str) -> bool:
        return bool(token)
'''
    parsed = parse_python_source(
        source,
        repository_id="fixture",
        revision="abc123",
        file_path="auth.py",
    )
    store = QdrantSemanticStore(
        url="http://qdrant:6333",
        collection="test",
        model_name="test-model",
    )

    documents = store._documents(parsed, source)

    assert [document.name for document in documents] == ["TokenValidator", "validate"]
    assert all(document.repository_id == "fixture" for document in documents)
    assert all(document.revision == "abc123" for document in documents)
    assert "bearer tokens" in documents[0].text


def test_semantic_delete_is_revision_and_file_scoped() -> None:
    store = QdrantSemanticStore(
        url="http://qdrant:6333",
        collection="test",
        model_name="test-model",
    )
    client = FakeQdrantClient()
    store._client = client

    store._delete_file("fixture", "revision-two", "removed.py")

    assert len(client.deletions) == 1
    assert client.deletions[0][0] == "test"
    conditions = client.deletions[0][1].must
    assert [condition.key for condition in conditions] == [
        "repository_id",
        "revision",
        "file_path",
    ]
