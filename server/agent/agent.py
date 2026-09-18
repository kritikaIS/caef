"""Agentic Core — plan, generate, verify, retry (TDD.md §2.4, LOOPS.md §2/§4).

The generation loop, in order, with no path that skips a step:

    retrieve (RAG) -> plan+generate (LLM, tool-mediated)
                   -> Guard Rail  -> Sandbox  -> AgentResult

Guard Rail and Sandbox failures are handled identically and share one retry
budget, `MAX_RETRIES` per `event_id` (SAFETY_PROTOCOL.md §4). Exhausting it
marks the event `escalated` and hands off to the Safety Rollback Protocol —
this component never decides to try "just once more".

The LLM is injected, not constructed here, so tests run against a scripted fake
with zero network calls (TDD.md §5).
"""

import json
import logging
import re
import uuid
from dataclasses import dataclass

import config
from server.agent import prompts
from server.agent.rag import retriever
from server.agent.tools import HardwareSchemaTool
from server.guardrail import guardrail
from server.sandbox import sandbox_runner
from server.schemas import (
    TOOL_CHECK_HARDWARE_SCHEMA,
    AgentOutput,
    AgentTask,
    GuardRailResult,
    SandboxResult,
    fw_hash,
    load_hardware_schema,
)

log = logging.getLogger("caef.agent")

# Models wrap JSON in fences even when told not to.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


@dataclass
class Attempt:
    """One trip through generate -> Guard Rail -> Sandbox, stored for audit."""

    number: int
    output: AgentOutput
    guardrail: GuardRailResult
    sandbox: SandboxResult | None  # None when Guard Rail rejected first


@dataclass
class AgentResult:
    task: AgentTask
    attempts: list[Attempt]
    output: AgentOutput | None  # the verified artifact, None if escalated
    escalated: bool

    @property
    def fw_hash(self) -> str | None:
        return fw_hash(self.output.code) if self.output else None


class GenerationError(RuntimeError):
    """The model returned something unusable. Counts as a failed attempt, not a
    pipeline crash — a bad model response must not take down the server."""


def parse_response(raw: str) -> tuple[str, str, list[int]]:
    """Extract (plan, code, pins_referenced) from the model's reply."""
    text = raw.strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GenerationError(f"response was not valid JSON: {exc}") from exc

    plan, code = payload.get("plan"), payload.get("code")
    if not isinstance(plan, str) or not plan.strip():
        raise GenerationError("response is missing a plan (FR-13 requires one)")
    if not isinstance(code, str) or not code.strip():
        raise GenerationError("response is missing code")

    pins = payload.get("pins_referenced", [])
    if not isinstance(pins, list) or not all(isinstance(p, int) for p in pins):
        raise GenerationError("pins_referenced must be a list of integers")
    return plan, code, pins


class Agent:
    """Model-agnostic. `llm` is anything with LangChain's `.invoke(messages)`."""

    def __init__(self, llm, sandbox=sandbox_runner) -> None:
        self.llm = llm
        self.sandbox = sandbox

    def generate(
        self, task: AgentTask, context: retriever.RetrievedContext, messages: list
    ) -> AgentOutput:
        """One planning+generation turn, with tool calls resolved in a loop.

        A fresh tool instance per attempt, so provenance never carries over
        between attempts: attempt 2 must re-check its own pins.
        """
        tool = HardwareSchemaTool(context.schema)
        bound = self.llm.bind_tools([tool.as_langchain_tool()])

        for _ in range(config.AGENT_MAX_TOOL_TURNS):
            reply = bound.invoke(messages)
            messages.append(reply)
            tool_calls = getattr(reply, "tool_calls", None) or []
            if not tool_calls:
                break
            for call in tool_calls:
                arguments = call.get("args", {})
                result = tool.check(int(arguments.get("pin_number")))
                messages.append(
                    {"role": "tool", "tool_call_id": call.get("id", ""), "content": result}
                )
        else:
            raise GenerationError(
                f"exceeded {config.AGENT_MAX_TOOL_TURNS} tool turns without producing code"
            )

        plan, code, pins = parse_response(_text_of(reply))
        return AgentOutput(
            patch_id=str(uuid.uuid4()),
            event_id=task.event_id,
            device_id=task.device_id,
            plan=plan,
            target_file=task.raw_payload.get("target_file", "main.py"),
            code=code,
            pins_referenced=pins,
            tool_calls=tool.calls,
        )

    def run(self, task: AgentTask, current_firmware: str) -> AgentResult:
        """The bounded loop. Returns either a verified artifact or an escalation."""
        context = retriever.retrieve(task, current_firmware)
        schema = load_hardware_schema(task.device_id)
        messages: list = [
            {"role": "system", "content": prompts.SYSTEM_PROMPT},
            {"role": "user", "content": prompts.initial_prompt(task, context)},
        ]
        attempts: list[Attempt] = []

        for number in range(1, config.MAX_RETRIES + 1):
            try:
                output = self.generate(task, context, messages)
            except GenerationError as exc:
                # A malformed response burns budget like any other failure —
                # otherwise a model stuck emitting prose loops forever.
                log.warning("attempt %s: %s", number, exc)
                messages.append(
                    {
                        "role": "user",
                        "content": prompts.retry_prompt(str(exc), number, config.MAX_RETRIES),
                    }
                )
                continue

            # NFR-1: Guard Rail runs unconditionally before anything executes.
            verdict = guardrail.check(output, schema)
            if verdict.status == "fail":
                log.info("attempt %s rejected by Guard Rail: %s", number, verdict.reason)
                attempts.append(Attempt(number, output, verdict, None))
                messages.append(
                    {
                        "role": "user",
                        "content": prompts.retry_prompt(
                            prompts.guardrail_feedback(verdict), number, config.MAX_RETRIES
                        ),
                    }
                )
                continue

            # Even Guard-Rail-passed code must still pass the Sandbox
            # (SAFETY_PROTOCOL.md §1 layer 4).
            result = self.sandbox.run(output, last_known_good=current_firmware)
            attempts.append(Attempt(number, output, verdict, result))
            if result.status == "pass":
                log.info("attempt %s verified: patch=%s", number, output.patch_id)
                return AgentResult(task=task, attempts=attempts, output=output, escalated=False)

            log.info("attempt %s failed Sandbox: %s", number, result.results)
            messages.append(
                {
                    "role": "user",
                    "content": prompts.retry_prompt(
                        prompts.sandbox_feedback(result), number, config.MAX_RETRIES
                    ),
                }
            )

        log.warning(
            "event %s exhausted %s attempts; escalating to Safety Rollback",
            task.event_id,
            config.MAX_RETRIES,
        )
        return AgentResult(task=task, attempts=attempts, output=None, escalated=True)


def _text_of(reply) -> str:
    """LangChain messages carry either a string or a list of content blocks."""
    content = getattr(reply, "content", reply)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block) for block in content
        )
    return str(content)


def build_llm():
    """Construct the configured chat model.

    Imported lazily so the whole pipeline — Guard Rail, Sandbox, rollback —
    stays importable and testable with no LLM SDK configured (NFR-4).
    """
    from langchain_openai import ChatOpenAI

    if not config.LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY is not set; the Agentic Core cannot start")
    return ChatOpenAI(
        model=config.LLM_MODEL,
        api_key=config.LLM_API_KEY,
        base_url=config.LLM_BASE_URL,
        temperature=config.LLM_TEMPERATURE,
    )
