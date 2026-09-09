from repo_chat.evaluation.golden import GoldenCase, evaluate_response, summarize_latencies


def test_golden_evaluation_accepts_grounded_semantic_response() -> None:
    case = GoldenCase(
        id="semantic",
        category="semantic",
        query="How are dependencies cached?",
        expected_agents=["graph", "code_analyst"],
        expected_source_paths_any=["fastapi/dependencies/utils.py"],
        answer_term_groups=[["cache"], ["depend"]],
    )

    result = evaluate_response(
        case,
        {
            "answer": "Dependency values use a request cache.",
            "trace_id": "trace-1",
            "partial": False,
            "agents_used": ["graph", "code_analyst"],
            "evidence": [
                {"location": {"file_path": "fastapi/dependencies/utils.py"}}
            ],
            "warnings": [],
        },
        response_status=200,
        latency_ms=10,
    )

    assert result.passed is True
    assert all(result.checks.values())


def test_golden_evaluation_reports_each_failed_contract() -> None:
    case = GoldenCase(
        id="failed",
        category="semantic",
        query="question",
        expected_agents=["graph", "code_analyst"],
        expected_source_paths_all=["expected.py"],
        answer_term_groups=[["required"]],
    )

    result = evaluate_response(
        case,
        {"answer": "unrelated", "partial": True, "agents_used": [], "evidence": []},
        response_status=503,
        latency_ms=20,
    )

    assert result.passed is False
    assert result.checks == {
        "http_200": False,
        "partial": False,
        "agents": False,
        "source_any": True,
        "source_all": False,
        "answer_terms": False,
        "trace_id": False,
    }


def test_latency_summary_uses_nearest_rank_percentiles() -> None:
    summary = summarize_latencies(
        [{"latency_ms": value} for value in (10, 20, 30, 40, 100)]
    )

    assert summary == {
        "minimum_ms": 10.0,
        "p50_ms": 30.0,
        "p95_ms": 100.0,
        "maximum_ms": 100.0,
    }


def test_latency_summary_handles_empty_results() -> None:
    assert summarize_latencies([]) == {
        "minimum_ms": 0.0,
        "p50_ms": 0.0,
        "p95_ms": 0.0,
        "maximum_ms": 0.0,
    }
