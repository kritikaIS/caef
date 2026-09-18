"""Scripted fake LLM (TDD.md §5: Agent tests use mocked responses, never a live
model, so CI is deterministic and free)."""

import json
from dataclasses import dataclass, field


@dataclass
class FakeReply:
    """Minimal stand-in for a LangChain AIMessage."""

    content: str = ""
    tool_calls: list = field(default_factory=list)


def tool_call(pin: int, call_id: str = "c1") -> dict:
    return {
        "id": call_id,
        "name": "check_hardware_schema",
        "args": {"pin_number": pin},
    }


def code_reply(plan: str, code: str, pins: list[int]) -> FakeReply:
    return FakeReply(
        content=json.dumps({"plan": plan, "code": code, "pins_referenced": pins})
    )


class FakeLLM:
    """Replays a scripted list of replies, one per invoke().

    Records every message list it was handed, so tests can assert on what the
    Agent actually fed back after a rejection.
    """

    def __init__(self, replies: list) -> None:
        self.replies = list(replies)
        self.invocations: list[list] = []
        self.bound_tools: list = []

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    def invoke(self, messages):
        self.invocations.append(list(messages))
        if not self.replies:
            raise AssertionError("FakeLLM ran out of scripted replies")
        return self.replies.pop(0)

    @property
    def prompts_seen(self) -> str:
        """Every user-role message the Agent has sent, concatenated."""
        texts = []
        for messages in self.invocations:
            for message in messages:
                if isinstance(message, dict) and message.get("role") in ("user", "tool"):
                    texts.append(str(message.get("content", "")))
        return "\n".join(texts)


class FakeSandbox:
    """Stands in for the Docker runner so Agent tests stay fast and offline."""

    def __init__(self, verdicts: list) -> None:
        self.verdicts = list(verdicts)
        self.ran: list = []

    def run(self, output, last_known_good=None):
        self.ran.append(output)
        if not self.verdicts:
            raise AssertionError("FakeSandbox ran out of scripted verdicts")
        verdict = self.verdicts.pop(0)
        return verdict.model_copy(update={"patch_id": output.patch_id})
