"""Agent prompts (TDD.md §2.4).

Two rules shape the system prompt:

  - **Plan before code** (FR-13). The plan is stored verbatim on the Patch row
    and is the auditable reasoning trace for NFR-5.
  - **No pin without a tool call** (FR-12/NFR-3). The prompt asks; Guard Rail
    enforces. The prompt is a hint to make the Agent succeed more often, never
    the safety mechanism — a prompt cannot be a safety mechanism.

Retry prompts feed back the Guard Rail reason or the Sandbox
`FAIL (Results, ΔFirmware)` payload identically, because they consume the same
retry budget (SAFETY_PROTOCOL.md §4).
"""

from server.agent.rag.retriever import RetrievedContext
from server.schemas import (
    TOOL_CHECK_HARDWARE_SCHEMA,
    AgentTask,
    GuardRailResult,
    SandboxResult,
    TriggerType,
)

SYSTEM_PROMPT = f"""You generate replacement firmware for a Raspberry Pi edge node.

HARD RULES — a violation is rejected mechanically before your code runs anywhere:

1. Call `{TOOL_CHECK_HARDWARE_SCHEMA}(pin_number)` for EVERY GPIO pin before you
   reference it. A pin used without a prior successful call is rejected.
2. Never reference a pin the tool reports FORBIDDEN or UNKNOWN.
3. Use the pre-vetted driver classes from `edge_node.drivers` instead of writing
   your own. Pass the pin as an argument; the drivers hold no pin literals.
4. Emit the COMPLETE replacement file, never a diff or a fragment. It must be
   valid, importable Python that runs standalone.
5. Keep the telemetry contract: log lines starting with `[firmware]`, and send
   `CONTEXT_TRIGGER` / `CRITICAL_FAILURE` telemetry the same way the current
   firmware does. A candidate that emits no `[firmware]` output fails
   verification even if it does not crash.
6. Read safety-relevant constants from `config`, never inline literals.
7. No `subprocess`, `eval`, `exec`, `ctypes`, no raw socket servers, and no
   `while True` without a `sleep` in the body.

You may REMOVE code. Stripping a nonessential driver to free CPU is an expected
and encouraged kind of change, not a regression.

Always produce a short natural-language plan BEFORE the code. The plan is stored
as the audit record of your reasoning."""

_OUTPUT_CONTRACT = """Respond with a JSON object and nothing else:

{
  "plan": "<2-4 sentences: what you are changing and why>",
  "code": "<the complete replacement file>",
  "pins_referenced": [<every GPIO pin number your code uses>]
}"""


def _context_block(context: RetrievedContext) -> str:
    parts = [
        "## Device hardware schema (READ-ONLY — physical reality, do not contradict)",
        context.schema.model_dump_json(indent=2),
        "",
        "## Firmware currently running on this device",
        "```python",
        context.current_firmware,
        "```",
    ]
    if context.driver_docs:
        parts += ["", "## Sensor Driver Library (pre-vetted — prefer these)", *context.driver_docs]
    if context.history_docs:
        parts += ["", "## Similar past events and the patches that resolved them",
                  *context.history_docs]
    return "\n".join(parts)


def initial_prompt(task: AgentTask, context: RetrievedContext) -> str:
    payload = task.raw_payload.get("data", {})
    if task.trigger_type is TriggerType.CRITICAL_FAILURE:
        goal = (
            "The device CRASHED. Identify the root cause from the stack trace and fix it.\n"
            "This fix is DURABLE — it will not be reverted, so it must be correct, not a\n"
            "workaround that suppresses the symptom.\n\n"
            f"Stack trace:\n{payload.get('trace', '(none provided)')}"
        )
    else:
        goal = (
            "The device reported a context change. Generate the MINIMUM firmware for this\n"
            "situation: enable what the situation needs, and remove what it does not, to\n"
            "free resources. This firmware is TEMPORARY — it is automatically reverted once\n"
            "the situation clears, so optimise for the situation, not for generality.\n\n"
            f"Event: {task.event}\nSensor data: {payload}"
        )

    return f"""{goal}

{_context_block(context)}

{_OUTPUT_CONTRACT}"""


def retry_prompt(reason: str, attempt: int, max_retries: int) -> str:
    return f"""Your previous attempt was REJECTED (attempt {attempt} of {max_retries}).

{reason}

Fix the specific problem above and emit the complete file again. Do not repeat
the same approach. If you referenced a pin, make sure you called
`{TOOL_CHECK_HARDWARE_SCHEMA}` for it in THIS attempt — the trace does not carry
over between attempts.

{_OUTPUT_CONTRACT}"""


def guardrail_feedback(result: GuardRailResult) -> str:
    failed = [name for name, status in result.checks.model_dump().items() if status == "fail"]
    return (
        f"Guard Rail rejected the code before it ran. Failed checks: {', '.join(failed)}.\n"
        f"Reason: {result.reason}"
    )


def sandbox_feedback(result: SandboxResult) -> str:
    """The literal FAIL (Results, ΔFirmware) artifact, handed back verbatim."""
    parts = [
        f"The Sandbox ran your code and it FAILED after {result.runtime_seconds}s "
        f"(exit code {result.exit_code}).",
        f"Results: {result.results}",
        f"Logs:\n{result.logs}",
    ]
    if result.delta_firmware:
        parts.append(f"ΔFirmware (your change vs last known-good):\n{result.delta_firmware}")
    return "\n\n".join(parts)
