"""Claude CLI harness for end-to-end git-curate invocation."""

from __future__ import annotations

import json
import random
import sys
import threading
from collections.abc import Iterable
from typing import Any

import sh

from ..common import ClaudeError, CLINotFoundError, Exit
from . import THINKING_MSGS, BaseHarness, build_prompt, console


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _error_message(event: Any) -> str:
    """Best-effort human-readable message from an error event.

    `error` may be an object with a `message`, or a bare string code (e.g. on
    assistant events, whose `message` is the assistant message object).
    """
    error = event.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    if isinstance(error, str) and error:
        return error
    message = event.get("message")
    if isinstance(message, str) and message:
        return message
    return str(event)


def _show_tool_use(block: Any) -> None:
    name: str = block.get("name", "")
    tool_input: dict[str, Any] = block.get("input") or {}
    if name == "Bash":
        console.print(f"\n[dim]$ {tool_input.get('command', '')}[/dim]")
    elif name in ("Write", "Edit"):
        console.print(f"\n[dim]\\[{name.lower()}] {tool_input.get('file_path', '')}[/dim]")
    elif name == "Read":
        path = tool_input.get("file_path", "")
        offset = tool_input.get("offset")
        limit = tool_input.get("limit")
        detail = f" (lines {offset}–{offset + limit})" if offset is not None and limit is not None else ""
        console.print(f"\n[dim]\\[read] {path}{detail}[/dim]")


def _stream_events(raw_event_stream: Iterable[str]) -> None:
    """Parse claude's streaming-json output and display it human-readably."""
    status = console.status(f"[dim]{random.choice(THINKING_MSGS)}…[/dim]")
    status.start()
    thinking = True
    lock = threading.Lock()
    stop_cycling = threading.Event()

    def _cycle() -> None:
        while not stop_cycling.wait(4.0):
            with lock:
                if thinking:
                    status.update(f"[dim]{random.choice(THINKING_MSGS)}…[/dim]")

    threading.Thread(target=_cycle, daemon=True).start()

    def _stop_thinking() -> None:
        nonlocal thinking
        with lock:
            if thinking:
                status.stop()
                thinking = False

    def _start_thinking() -> None:
        nonlocal thinking
        with lock:
            if not thinking:
                status.update(f"[dim]{random.choice(THINKING_MSGS)}…[/dim]")
                status.start()
                thinking = True

    try:
        for raw_event in raw_event_stream:
            raw_event = raw_event.strip()
            if not raw_event:
                continue
            try:
                event: Any = json.loads(raw_event)
            except json.JSONDecodeError:
                _stop_thinking()
                print(raw_event, flush=True)
                continue

            event_type: str = event.get("type", "")

            if event_type == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    block_type: str = block.get("type", "")
                    if block_type == "text":
                        text: str = block.get("text", "")
                        if text:
                            _stop_thinking()
                            print(text, end="", flush=True)
                    elif block_type == "tool_use":
                        _stop_thinking()
                        _show_tool_use(block)
                        _start_thinking()
                if event.get("error"):
                    _stop_thinking()
                    print(f"\nclaude: {_error_message(event)}", file=sys.stderr, flush=True)
                    raise ClaudeError()

            elif event_type == "error":
                _stop_thinking()
                print(f"\nclaude: {_error_message(event)}", file=sys.stderr, flush=True)
                raise ClaudeError()

            elif event_type == "system":
                subtype: str = event.get("subtype", "")
                if subtype == "error_during_execution":
                    _stop_thinking()
                    print(f"\nclaude: {_error_message(event)}", file=sys.stderr, flush=True)
                    raise ClaudeError()

            elif event_type == "result":
                _stop_thinking()
                if event.get("is_error"):
                    print(f"\nclaude: {event.get('result', '')}", file=sys.stderr, flush=True)
                    raise ClaudeError()
                else:
                    cost = event.get("total_cost_usd")
                    usage = event.get("usage") or {}
                    if cost is not None:
                        input_tokens = usage.get("input_tokens", 0)
                        cache_write = usage.get("cache_creation_input_tokens", 0)
                        cache_read = usage.get("cache_read_input_tokens", 0)
                        output_tokens = usage.get("output_tokens", 0)
                        total_in = input_tokens + cache_write + cache_read
                        cache_note = ""
                        if cache_write or cache_read:
                            cache_note = (
                                f", {_fmt_tokens(cache_read)} cache read, {_fmt_tokens(cache_write)} cache write"
                            )
                        msg = (
                            f"[cost] ${cost:.4f}  ({_fmt_tokens(total_in)} in"
                            f" / {_fmt_tokens(output_tokens)} out{cache_note})"
                        )
                        print(f"\n\033[2m{msg}\033[0m", flush=True)

    finally:
        stop_cycling.set()
        _stop_thinking()


class ClaudeHarness(BaseHarness):
    def _run(self, base_sha: str, repo_root: str, temp_dir: str, spec_path: str) -> None:
        args = self.build_args(build_prompt(base_sha, spec_path), temp_dir)
        try:
            # _in=os.devnull: claude detects non-terminal stdin and exits cleanly
            # after the task instead of waiting for further user input.
            import os

            proc = sh.claude(
                *args,
                _cwd=repo_root,
                _in=os.devnull,
                _err=sys.stderr,
                _iter=True,
                # Raise a failed exit only here, when iteration ends, not also from sh's
                # background thread, which would print a traceback.
                _bg_exc=False,
            )
            _stream_events(proc)
        except sh.CommandNotFound as e:
            print("error: 'claude' CLI not found. Install Claude Code to use this harness.", file=sys.stderr)
            raise CLINotFoundError() from e
        except sh.ErrorReturnCode as e:
            print("\nclaude exited with an error, re-running to capture output…", file=sys.stderr, flush=True)
            try:
                sh.claude(*args, _cwd=repo_root, _in=os.devnull)
            except sh.ErrorReturnCode as e2:
                for stream in (e2.stdout, e2.stderr):
                    if stream:
                        print(stream.decode(errors="replace"), file=sys.stderr, flush=True)
            raise Exit(e.exit_code) from e

    def build_args(self, prompt: str, temp_dir: str) -> list[str]:
        # temp_dir starts with "/", so "/{temp_dir}" becomes "//absolute/path/**"
        # which is the gitignore-style absolute-path pattern Claude requires.
        file_pattern = f"/{temp_dir}/**"
        return [
            "--allowedTools",
            ",".join(
                [
                    "Bash(uvx git-curate *)",
                    "Bash(git log *)",
                    "Read",
                    f"Write({file_pattern})",
                    f"Edit({file_pattern})",
                    f"Create({file_pattern})",
                ]
            ),
            "--output-format",
            "stream-json",
            # Need --verbose when mixing stream-json and -p:
            "--verbose",
            *self.model_args(),
            "-p",
            prompt,
        ]
