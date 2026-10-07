"""pi harness for end-to-end git-curate invocation."""

from __future__ import annotations

import json
import os
import random
import sys
import threading
from collections.abc import Iterable

import sh

from ..common import CLINotFoundError, Exit
from . import THINKING_MSGS, BaseHarness, build_prompt, console


def _show_tool(name: str, args: dict) -> None:
    if name == "bash":
        print(f"\n\033[2m$ {args.get('command', '')}\033[0m", flush=True)
    elif name in ("write", "edit"):
        print(f"\n\033[2m[{name}] {args.get('path', '')}\033[0m", flush=True)
    elif name == "read":
        path = args.get("path", "")
        offset = args.get("offset")
        limit = args.get("limit")
        detail = f" (lines {offset}–{offset + limit})" if offset is not None and limit is not None else ""
        print(f"\n\033[2m[read] {path}{detail}\033[0m", flush=True)
    else:
        print(f"\n\033[2m[{name}]\033[0m", flush=True)


def _stream_events(stream: Iterable[str]) -> None:
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

    text_block_has_content = False

    try:
        for raw in stream:
            raw = raw.strip()
            if not raw:
                continue
            try:
                event: dict = json.loads(raw)
            except json.JSONDecodeError:
                continue

            event_type: str = event.get("type", "")

            if event_type == "message_update":
                assistant_event: dict = event.get("assistantMessageEvent") or {}
                assistant_event_type: str = assistant_event.get("type", "")
                if assistant_event_type == "text_start":
                    text_block_has_content = False
                elif assistant_event_type == "text_delta":
                    delta: str = assistant_event.get("delta", "")
                    if delta:
                        if delta.strip():
                            text_block_has_content = True
                            _stop_thinking()
                            print(delta, end="", flush=True)
                        elif text_block_has_content:
                            print(delta, end="", flush=True)

            elif event_type == "tool_execution_start":
                text_block_has_content = False
                _stop_thinking()
                _show_tool(event.get("toolName", ""), event.get("args") or {})
                _start_thinking()

    finally:
        stop_cycling.set()
        _stop_thinking()


class PiHarness(BaseHarness):
    def _run(self, base_sha: str, repo_root: str, temp_dir: str, spec_path: str) -> None:
        args = self.build_args(build_prompt(base_sha, spec_path))
        try:
            proc = sh.pi(
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
            print("error: 'pi' CLI not found. Install pi to use this harness.", file=sys.stderr)
            raise CLINotFoundError() from e
        except sh.ErrorReturnCode as e:
            raise Exit(e.exit_code) from e

    def build_args(self, prompt: str) -> list[str]:
        return [
            "--tools",
            "read,bash,edit,write",
            "--mode",
            "json",
            *self.model_args(),
            "-p",
            prompt,
        ]
