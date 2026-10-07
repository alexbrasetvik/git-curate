"""Tests for parsing the claude CLI's stream-json output."""

from __future__ import annotations

import json

import pytest

from git_curate.common import ClaudeError
from git_curate.harness.claude import _stream_events


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        # Assistant events carry a bare string error code; `message` is the assistant message object.
        (
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "There's an issue with the selected model."}]},
                "error": "invalid_request",
            },
            "claude: invalid_request",
        ),
        ({"type": "error", "error": {"message": "overloaded"}}, "claude: overloaded"),
        ({"type": "error", "error": "rate_limited"}, "claude: rate_limited"),
        ({"type": "error", "message": "boom"}, "claude: boom"),
        ({"type": "system", "subtype": "error_during_execution", "error": "crashed"}, "claude: crashed"),
    ],
)
def test_error_events_exit_cleanly(event: dict, expected: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(ClaudeError):
        _stream_events([json.dumps(event)])
    assert expected in capsys.readouterr().err
