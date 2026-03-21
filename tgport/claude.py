import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from collections.abc import AsyncGenerator

from . import config

logger = logging.getLogger(__name__)


class SessionNotFoundError(Exception):
    pass


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolUse:
    tool: str
    input: dict | None = None


@dataclass
class Result:
    text: str
    cost_usd: float
    is_error: bool
    errors: list[str]
    usage: dict | None = None
    session_id: str | None = None


@dataclass
class Error:
    message: str


StreamEvent = TextDelta | ToolUse | Result | Error


def _build_command(
    prompt: str,
    session_id: uuid.UUID,
    is_new_session: bool,
) -> list[str]:
    cmd = [
        "claude",
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--max-budget-usd", str(config.CLAUDE_MAX_BUDGET_USD),
    ]
    if config.CLAUDE_SKIP_PERMISSIONS:
        cmd.append("--dangerously-skip-permissions")
    if config.CLAUDE_MODEL:
        cmd += ["--model", config.CLAUDE_MODEL]
    if config.CLAUDE_EFFORT:
        cmd += ["--effort", config.CLAUDE_EFFORT]
    if config.CLAUDE_MAX_TURNS:
        cmd += ["--max-turns", str(config.CLAUDE_MAX_TURNS)]
    if is_new_session:
        cmd += ["--session-id", str(session_id)]
    else:
        cmd += ["--resume", str(session_id)]
    cmd.append(prompt)
    return cmd


def _parse_event(data: dict) -> list[StreamEvent]:
    """Parse a stream-json event into zero or more StreamEvents."""
    etype = data.get("type")
    events: list[StreamEvent] = []

    if etype == "content_block_delta":
        delta = data.get("delta", {})
        if delta.get("type") == "text_delta":
            events.append(TextDelta(text=delta.get("text", "")))

    elif etype == "content_block_start":
        cb = data.get("content_block", {})
        if cb.get("type") == "tool_use":
            events.append(ToolUse(tool=cb.get("name", "unknown"), input=cb.get("input")))

    elif etype == "assistant":
        msg = data.get("message", {})
        for block in msg.get("content", []):
            if block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    events.append(TextDelta(text=text))
            elif block.get("type") == "tool_use":
                events.append(ToolUse(tool=block.get("name", "unknown"), input=block.get("input")))

    elif etype == "result":
        result = data.get("result", "")
        cost = data.get("total_cost_usd", 0.0)
        is_error = data.get("is_error", False)
        errors = data.get("errors", [])
        usage = data.get("modelUsage")
        sid = data.get("session_id")
        events.append(Result(text=result, cost_usd=cost, is_error=is_error, errors=errors, usage=usage, session_id=sid))

    return events


async def stream_claude(
    prompt: str,
    session_id: uuid.UUID,
    is_new_session: bool,
) -> AsyncGenerator[StreamEvent, None]:
    cmd = _build_command(prompt, session_id, is_new_session)

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=config.CLAUDE_WORK_DIR,
    )

    loop = asyncio.get_running_loop()
    deadline = loop.time() + config.RESPONSE_TIMEOUT
    stderr_output = ""
    got_result = False

    try:
        assert process.stdout
        async for raw_line in process.stdout:
            if loop.time() > deadline:
                process.kill()
                yield Error(message="Response timed out.")
                return

            line = raw_line.decode().strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                for event in _parse_event(data):
                    if isinstance(event, Result):
                        got_result = True
                    yield event
            except json.JSONDecodeError:
                logger.debug("Non-JSON output from claude: %s", line[:200])
                continue

    except Exception as e:
        process.kill()
        yield Error(message=str(e))

    finally:
        try:
            await asyncio.wait_for(process.wait(), timeout=30)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

        if process.stderr:
            stderr_output = (await process.stderr.read()).decode().strip()

        # Don't raise if we already got a result from stdout
        if got_result:
            if process.returncode and process.returncode != 0:
                logger.debug("Claude CLI exited with code %d after result (ignored)", process.returncode)
            return

        if process.returncode and process.returncode != 0:
            if stderr_output:
                logger.error("Claude CLI exited with code %d: %s", process.returncode, stderr_output[:500])
            if "session" in stderr_output.lower() or "resume" in stderr_output.lower():
                raise SessionNotFoundError(stderr_output)
            if not stderr_output:
                stderr_output = f"Claude CLI exited with code {process.returncode}"
            raise RuntimeError(stderr_output)
