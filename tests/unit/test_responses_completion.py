"""Responses completion usage accounting stays credential-free."""

from __future__ import annotations

from scripts.responses_completion import _usage_record


def test_usage_record_keeps_tokens_but_not_prompt_or_credentials() -> None:
    record = _usage_record(
        {
            "usage": {
                "input_tokens": 11,
                "output_tokens": 7,
                "total_tokens": 18,
                "input_tokens_details": {"cached_tokens": 3},
            },
            "input": "secret prompt",
        },
        "deepseek-flash",
    )
    assert record == {
        "source": "responses-api",
        "model": "deepseek-flash",
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
        "cached_input_tokens": 3,
    }


def test_missing_usage_is_explicitly_null() -> None:
    record = _usage_record({}, "deepseek-flash")
    assert record["input_tokens"] is None
    assert record["output_tokens"] is None
