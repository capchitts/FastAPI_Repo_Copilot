# FastAPI Repository Chat Agent - MCP Multi-Agent System

## Overview

Build a production-ready multi-agent system that can answer questions about the FastAPI codebase. The system must be architected using the Model Context Protocol (MCP), with specialized agents handling distinct responsibilities. This assignment tests your ability to design distributed agent systems, implement MCP servers, work with graph databases, and build scalable AI architectures.

## Objective

Design and implement a multi-agent system where:

- Each agent is an independent MCP server with specialized capabilities
- Agents collaborate through a central orchestrator to answer complex queries
- The FastAPI repository is indexed into a shared knowledge graph
- The system handles multi-turn conversations with context retention
- Responses are synthesized from multiple agent outputs

## System Architecture

### Required Agents

You must implement the following five MCP servers:

#### 1. Orchestrator Agent

The central coordinator that routes queries and synthesizes responses.

**Responsibilities:**
- Analyze incoming queries and determine which agents to invoke
- Manage conversation context and memory
- Coordinate parallel or sequential agent calls
- Synthesize final responses from multiple agent outputs
- Handle fallback strategies when agents fail

**Required MCP Tools:**
- `analyze_query` - Classify query intent and extract key entities
- `route_to_agents` - Determine which agents should handle the query
- `get_conversation_context` - Retrieve relevant conversation history
- `synthesize_response` - Combine agent outputs into coherent response


#### 2. Indexer Agent

Handles repository parsing and knowledge graph population.

**Responsibilities:**
- Clone and parse the FastAPI repository
- Extract AST information from Python files
- Identify classes, functions, methods, and their relationships
- Populate the Neo4j knowledge graph
- Handle incremental updates

**Required MCP Tools:**
- `index_repository` - Full repository indexing
- `index_file` - Single file indexing
- `parse_python_ast` - Extract AST from Python code
- `extract_entities` - Identify code entities and relationships
- `get_index_status` - Report indexing progress and statistics


#### 3. Graph Query Agent

Specializes in knowledge graph traversal and relationship queries.

**Responsibilities:**
- Execute Cypher queries against Neo4j
- Find relationships between code entities
- Trace import chains and dependencies
- Identify usage patterns across the codebase

**Required MCP Tools:**
- `find_entity` - Locate a class, function, or module by name
- `get_dependencies` - Find what an entity depends on
- `get_dependents` - Find what depends on an entity
- `trace_imports` - Follow import chain for a module
- `find_related` - Get entities related by specified relationship type
- `execute_query` - Run custom Cypher query (with safety constraints)

#### 4. Code Analyst Agent

Provides deep code understanding and pattern analysis.

**Responsibilities:**
- Analyze function implementations
- Detect design patterns
- Extract code snippets with context
- Explain complex code logic
- Identify best practices and anti-patterns

**Required MCP Tools:**
- `analyze_function` - Deep analysis of a function's logic
- `analyze_class` - Comprehensive class analysis
- `find_patterns` - Detect design patterns in code
- `get_code_snippet` - Extract code with surrounding context
- `explain_implementation` - Generate explanation of how code works
- `compare_implementations` - Compare two code entities



### Shared Infrastructure

#### Knowledge Graph (Neo4j)

Design a schema that captures:

- **Nodes:** Module, Class, Function, Method, Parameter, Decorator, Import, Docstring, File
- **Relationships:** CONTAINS, IMPORTS, INHERITS_FROM, CALLS, DECORATED_BY, HAS_PARAMETER, DOCUMENTED_BY, DEPENDS_ON

#### Conversation Memory

Store and manage:

- Conversation history per session
- Agent response cache
- Query routing decisions
- User preferences and context

### Communication Flow

```
User Query
     |
     v
[FastAPI Gateway]
     |
     v
[Orchestrator Agent]
     |
     +---> [Indexer Agent] ----+
     |                         |
     +---> [Graph Query Agent] +---> [Neo4j]
     |                         |
     +---> [Code Analyst Agent]+
     |
     v
[Response Synthesis]
     |
     v
User Response
```

## API Requirements

### FastAPI Gateway

Implement a FastAPI application that serves as the external interface:

**Endpoints:**

- `POST /api/chat` - Send message, receive response
  - Support for session management
  - Streaming response option
  
- `POST /api/index` - Trigger repository indexing
  - Support for full and incremental indexing
  - Return job ID for status tracking
  
- `GET /api/index/status/{job_id}` - Get indexing job status

- `GET /api/agents/health` - Health check for all agents

- `GET /api/graph/statistics` - Knowledge graph statistics

- `WebSocket /ws/chat` - Real-time chat with streaming

## Configuration Requirements

- Use Pydantic Settings for all configuration
- Environment-specific configurations (development, testing, production)
- Each MCP server must have independent configuration
- Secrets management for API keys and database credentials
- Configurable agent timeouts and retry policies

## Code Quality Standards

- Type hints throughout the codebase
- Comprehensive docstrings (Google or NumPy style)
- Custom exception hierarchy for agent errors
- Structured logging with correlation IDs across agents
- Input validation using Pydantic models
- Clean code principles (SOLID, DRY)
- Async/await patterns for all I/O operations

## Evaluation Criteria

### Multi-Agent Architecture (35%)

- Clear agent responsibility boundaries
- Effective orchestration strategy
- Proper MCP protocol implementation
- Inter-agent communication design
- Failure handling and fallback strategies
- Scalability considerations

### Code Quality (25%)

- Type safety and type hints
- Error handling across agent boundaries
- Code readability and documentation
- Consistent patterns across agents
- Testing coverage (aim for >70%)

### Functionality (25%)

- Accurate repository indexing
- Effective knowledge graph schema
- Quality of agent tool implementations
- Response accuracy and relevance
- Context management across turns

### Production Readiness (15%)

- Docker Compose setup for all services
- Configuration management
- Logging and observability
- API documentation
- Security considerations

## Deliverables

### 1. Source Code

- Complete implementation of all five MCP servers
- FastAPI gateway application
- Shared infrastructure code
- All dependencies specified in `pyproject.toml`

### 2. README.md

- Architecture overview with diagrams
- Setup and installation instructions
- Individual agent documentation
- API documentation
- Design decisions and trade-offs
- Known limitations and future improvements


### 3. Docker Configuration

- Dockerfile for each MCP server
- Dockerfile for API gateway
- Docker Compose file orchestrating all services
- Health checks and dependency ordering

### 4. Environment Configuration

- `.env.example` with all required variables
- Documentation of configuration options per agent


## Sample Queries to Test

Your multi-agent system should handle queries like:

**Simple (single agent):**
- "What is the FastAPI class?"
- "Show me the docstring for the Depends function"

**Medium (2-3 agents):**
- "How does FastAPI handle request validation?"
- "What classes inherit from APIRouter?"
- "Find all decorators used in the routing module"

**Complex (multiple agents + synthesis):**
- "Explain the complete lifecycle of a FastAPI request"
- "How does dependency injection work and show me examples from the codebase"
- "Compare how Path and Query parameters are implemented"
- "What design patterns are used in FastAPI's core and why?"

## Submission Guidelines

1. Create a private GitHub repository
2. Include all source code, tests, and documentation
3. Ensure all services start with `docker-compose up`
4. Provide a walkthrough (video 10-15 min or written document) demonstrating:
   - System architecture and agent design decisions
   - Setup and installation process
   - Indexing process and knowledge graph population
   - Example queries showing multi-agent collaboration
   - How agents communicate and synthesize responses
   - Monitoring and observability features


---

**Good luck! We're excited to see your multi-agent architecture.**
