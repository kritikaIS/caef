"""Agent tools — the only sanctioned hardware access (TDD.md §2.4, FR-12).

`check_hardware_schema(pin_number)` is the sole way the Agent learns whether a
pin is safe and what is attached to it. Guard Rail cross-checks every pin
literal in generated code against a logged successful call to this tool for the
same `patch_id` (SAFETY_PROTOCOL.md §1 layer 2).

**Read-only by construction.** There is no write counterpart in this module and
none anywhere else in the pipeline — the Agent cannot rewrite physical reality
because no code path exists to do it, not because a prompt asks it not to
(CLAUDE.md §4, SAFETY_PROTOCOL.md §1 layer 1).
"""

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from server.schemas import (
    TOOL_CHECK_HARDWARE_SCHEMA,
    TOOL_RESULT_SAFE_PREFIX,
    HardwareSchema,
    ToolCallRecord,
)


class CheckHardwareSchemaArgs(BaseModel):
    pin_number: int = Field(description="GPIO pin number to check, e.g. 27 for GPIO_27")


class HardwareSchemaTool:
    """Stateful wrapper: answers pin questions and records the trace.

    The trace is the audit artifact Guard Rail consumes, so it is captured here
    at the point of truth rather than reconstructed from model output — an Agent
    cannot fabricate provenance for a call it never made.
    """

    def __init__(self, schema: HardwareSchema) -> None:
        self.schema = schema
        self.calls: list[ToolCallRecord] = []

    def check(self, pin_number: int) -> str:
        entry = self.schema.pin(pin_number)

        if self.schema.is_forbidden(pin_number):
            result = (
                f"FORBIDDEN: GPIO_{pin_number} is on the device denylist and must not "
                f"appear in generated code under any circumstance."
            )
        elif entry is None:
            result = (
                f"UNKNOWN: GPIO_{pin_number} is not present in this device's hardware "
                f"schema. Nothing is wired to it; do not reference it."
            )
        else:
            result = (
                f"{TOOL_RESULT_SAFE_PREFIX}: Connected to {entry.connected_device} "
                f"(type={entry.type}, status={entry.status}"
                + (f", protocol={entry.protocol}" if entry.protocol else "")
                + (f", active_level={entry.active_level}" if entry.active_level else "")
                + ")"
            )

        self.calls.append(
            ToolCallRecord(
                tool=TOOL_CHECK_HARDWARE_SCHEMA,
                args={"pin_number": pin_number},
                result=result,
            )
        )
        return result

    def as_langchain_tool(self) -> StructuredTool:
        return StructuredTool.from_function(
            func=self.check,
            name=TOOL_CHECK_HARDWARE_SCHEMA,
            description=(
                "Check whether a GPIO pin is safe to use and what hardware is wired "
                "to it. You MUST call this for every pin before referencing it in "
                "generated code. Returns SAFE / FORBIDDEN / UNKNOWN."
            ),
            args_schema=CheckHardwareSchemaArgs,
        )
