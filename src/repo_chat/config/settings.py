from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TESTING = "testing"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REPO_CHAT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = Environment.DEVELOPMENT
    service_name: str = "repository-chat"
    log_level: str = "INFO"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=8001, ge=1, le=65535)
    graph_agent_port: int = Field(default=8002, ge=1, le=65535)
    code_analyst_port: int = Field(default=8003, ge=1, le=65535)
    orchestrator_port: int = Field(default=8004, ge=1, le=65535)
    repository_agent_port: int = Field(default=8005, ge=1, le=65535)
    gateway_host: str = "127.0.0.1"
    gateway_port: int = Field(default=8000, ge=1, le=65535)
    conversation_maximum_turns: int = Field(default=10, ge=1, le=100)
    conversation_ttl_seconds: int = Field(default=86400, ge=60)
    response_cache_ttl_seconds: int = Field(default=3600, ge=60)
    routing_audit_ttl_seconds: int = Field(default=604800, ge=60)
    routing_audit_maximum_records: int = Field(default=100, ge=1, le=1000)
    index_job_ttl_seconds: int = Field(default=604800, ge=60)
    index_lock_ttl_seconds: int = Field(default=7200, ge=60)
    indexer_mcp_url: str = "http://127.0.0.1:8001/mcp"
    graph_agent_mcp_url: str = "http://127.0.0.1:8002/mcp"
    code_analyst_mcp_url: str = "http://127.0.0.1:8003/mcp"
    orchestrator_mcp_url: str = "http://127.0.0.1:8004/mcp"
    repository_agent_mcp_url: str = "http://127.0.0.1:8005/mcp"
    source_maximum_file_bytes: int = Field(default=1_000_000, ge=1)
    source_maximum_snippet_lines: int = Field(default=40, ge=5, le=200)

    redis_url: str = "redis://localhost:6379/0"

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: SecretStr = Field(default=SecretStr("development-only"))
    neo4j_database: str = "neo4j"

    repository_root: Path = Path("/data/repositories")
    git_allowed_hosts: str = "github.com,gitlab.com,bitbucket.org"
    git_command_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    git_maximum_snapshot_bytes: int = Field(default=500_000_000, ge=1_000_000)

    semantic_search_enabled: bool = True
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr | None = None
    qdrant_collection: str = "repository_code"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_cache_dir: Path = Path("/tmp/repo-chat-models")
    semantic_chunk_maximum_characters: int = Field(default=4_000, ge=500, le=20_000)
    semantic_search_limit: int = Field(default=12, ge=1, le=100)

    agent_timeout_seconds: float = Field(default=10.0, gt=0)
    agent_max_retries: int = Field(default=2, ge=0, le=5)
    agent_retry_base_seconds: float = Field(default=0.1, ge=0, le=10)
    semantic_agent_timeout_seconds: float = Field(default=120.0, gt=0, le=300)
    gateway_orchestrator_timeout_seconds: float = Field(default=180.0, gt=0, le=600)
    gateway_maximum_concurrent_chats: int = Field(default=32, ge=1, le=10_000)

    llm_enabled: bool = False
    llm_api_key: SecretStr | None = None
    llm_model: str = "openai/gpt-oss-20b"
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    llm_maximum_input_characters: int = Field(default=20_000, ge=1_000, le=200_000)
    llm_maximum_output_tokens: int = Field(default=1_200, ge=100, le=10_000)


@lru_cache
def get_settings() -> Settings:
    return Settings()
