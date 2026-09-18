"""M6 check: retrieval grounds the Agent, tools mediate every pin, and the
retry loop is bounded and escalates rather than looping.

No live LLM anywhere (TDD.md §5) — every model reply is scripted.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from server.agent import prompts  # noqa: E402
from server.agent.agent import Agent, GenerationError, parse_response  # noqa: E402
from server.agent.rag import indexer, retriever  # noqa: E402
from server.agent.tools import HardwareSchemaTool  # noqa: E402
from server.db import models as m  # noqa: E402
from server.schemas import (  # noqa: E402
    TOOL_CHECK_HARDWARE_SCHEMA,
    AgentTask,
    SandboxResult,
    TriggerType,
    load_hardware_schema,
)
from tests.fakes import FakeLLM, FakeSandbox, FakeReply, code_reply, tool_call  # noqa: E402

SCHEMA = load_hardware_schema("dev01")

MORPH_CODE = """import time

import config
from edge_node import telemetry
from edge_node.drivers import DHT11, RelayFan
from server.schemas import TriggerType


def sensor_loop():
    temp_sensor = DHT11(pin=17)
    fan = RelayFan(pin=27)
    fan.on()
    while True:
        temp_c = temp_sensor.read_temp_c()
        print(f"[firmware] temp={temp_c}C fan=ON", flush=True)
        time.sleep(config.SENSOR_TICK_SECONDS)


sensor_loop()
"""

BASELINE = (ROOT / "edge_node" / "main.py").read_text()


@pytest.fixture(autouse=True)
def rag_index(tmp_path, monkeypatch):
    """Fresh RAG index per test; the corpus is rebuilt from real sources."""
    monkeypatch.setattr(config, "RAG_DB_PATH", tmp_path / "rag.db")
    m.init_db()
    indexer.reindex("pi_node_alpha")
    yield
    with m.SessionLocal() as db:
        for table in (m.HistoryRecord, m.Patch, m.Event, m.Device):
            db.query(table).delete()
        db.commit()


def heat_task() -> AgentTask:
    return AgentTask(
        task_id="t1",
        event_id="e1",
        device_id="pi_node_alpha",
        trigger_type=TriggerType.CONTEXT_TRIGGER,
        event="HIGH_HEAT_DETECTED",
        raw_payload={"data": {"temp_c": 85.4, "threshold": 80.0}},
    )


def crash_task() -> AgentTask:
    return AgentTask(
        task_id="t2",
        event_id="e2",
        device_id="pi_node_alpha",
        trigger_type=TriggerType.CRITICAL_FAILURE,
        event="UNHANDLED_EXCEPTION",
        raw_payload={"data": {"trace": "IndexError: list index out of range"}},
    )


def passing() -> SandboxResult:
    return SandboxResult(patch_id="x", status="pass", runtime_seconds=10.0, exit_code=0, logs="ok")


def failing(logs="Traceback ... IndexError") -> SandboxResult:
    return SandboxResult(
        patch_id="x",
        status="fail",
        runtime_seconds=2.0,
        exit_code=1,
        logs=logs,
        results="Process exited with code 1 after 2s",
        delta_firmware="- old\n+ new",
    )


def good_replies(code=MORPH_CODE):
    """Model checks both pins, then emits code — the well-behaved sequence."""
    return [
        FakeReply(tool_calls=[tool_call(17, "c1"), tool_call(27, "c2")]),
        code_reply("Enable Relay_Fan on GPIO_27, drop Lidar to free CPU.", code, [17, 27]),
    ]


# --- tools -------------------------------------------------------------------


def test_tool_reports_safe_forbidden_and_unknown():
    tool = HardwareSchemaTool(SCHEMA)
    assert tool.check(27).startswith("SAFE")
    assert "Relay_Fan" in tool.calls[0].result
    assert tool.check(0).startswith("FORBIDDEN")
    assert tool.check(99).startswith("UNKNOWN")
    assert [c.args["pin_number"] for c in tool.calls] == [27, 0, 99]


def test_tool_has_no_write_path():
    """SAFETY_PROTOCOL.md §1 layer 1 is structural, not prompt-based."""
    surface = [name for name in dir(HardwareSchemaTool) if not name.startswith("_")]
    assert surface == ["as_langchain_tool", "check"]


# --- retrieval ---------------------------------------------------------------


def test_retrieval_grounds_the_agent_in_real_hardware():
    """FR-11: schema and current firmware are fetched directly, never ranked."""
    context = retriever.retrieve(heat_task(), BASELINE)
    assert context.schema.device_id == "pi_node_alpha"
    assert context.schema.pin(27).connected_device == "Relay_Fan"
    assert context.current_firmware == BASELINE
    assert any("RelayFan" in doc for doc in context.driver_docs)


def test_retrieval_surfaces_only_deployed_history():
    """A rejected attempt is not precedent; surfacing it invites a repeat."""
    with m.SessionLocal() as db:
        db.add(m.Device(id="pi_node_alpha", mcu_type="RaspberryPi_4B"))
        event = m.Event(
            device_id="pi_node_alpha",
            trigger_type=TriggerType.CONTEXT_TRIGGER,
            event="HIGH_HEAT_DETECTED",
            timestamp=1,
            current_state_hash="h",
            data={"temp_c": 90.0},
        )
        db.add(event)
        db.flush()
        good = m.Patch(
            event_id=event.id, device_id="pi_node_alpha", plan="Enable the fan on GPIO_27.",
            target_file="main.py", code=MORPH_CODE, pins_referenced=[27], fw_hash="good",
        )
        bad = m.Patch(
            event_id=event.id, device_id="pi_node_alpha", plan="Overclock the CPU instead.",
            target_file="main.py", code="boom", pins_referenced=[], fw_hash="bad",
        )
        db.add_all([good, bad])
        db.flush()
        db.add_all([
            m.HistoryRecord(time=1, device_id="pi_node_alpha", event_id=event.id,
                            patch_id=good.id, fw_hash="good",
                            record_type="morph_deploy", status="deployed"),
            m.HistoryRecord(time=2, device_id="pi_node_alpha", event_id=event.id,
                            patch_id=bad.id, fw_hash="bad",
                            record_type="morph_deploy", status="failed"),
        ])
        db.commit()

    indexer.reindex("pi_node_alpha")
    context = retriever.retrieve(heat_task(), BASELINE)
    combined = "\n".join(context.history_docs)
    assert "Enable the fan on GPIO_27" in combined
    assert "Overclock" not in combined


def test_retrieval_survives_an_empty_index(tmp_path, monkeypatch):
    """Missing history must degrade, not crash: schema still reaches the Agent."""
    monkeypatch.setattr(config, "RAG_DB_PATH", tmp_path / "empty.db")
    context = retriever.retrieve(heat_task(), BASELINE)
    assert context.schema.device_id == "pi_node_alpha"
    assert context.history_docs == []


def test_retrieval_indexes_the_driver_library_on_a_cold_start(tmp_path, monkeypatch):
    """FR-11: the Agent must never plan against an absent driver library.

    Nothing outside the tests used to build the corpus, so a real run retrieved
    zero drivers and the Agent invented driver APIs — `RelayFan.turn_off()` for
    a driver whose method is `off()`. Guard Rail passes invented calls (they are
    neither forbidden pins nor denylisted), the Sandbox fails them, and three
    attempts later the device is rolled back for a bug in the prompt.
    """
    monkeypatch.setattr(config, "RAG_DB_PATH", tmp_path / "cold.db")

    context = retriever.retrieve(heat_task(), BASELINE)

    combined = "\n".join(context.driver_docs)
    assert "class RelayFan" in combined, "the driver library must reach the Agent"
    # The exact API the Agent got wrong when it had to guess.
    assert "def off" in combined and "turn_off" not in combined


# --- prompts -----------------------------------------------------------------


def test_prompt_distinguishes_temporary_morph_from_durable_patch():
    """LOOPS.md §2 vs §4: a morph reverts, a patch does not — and the Agent has
    to know which it is writing."""
    context = retriever.retrieve(heat_task(), BASELINE)
    morph = prompts.initial_prompt(heat_task(), context)
    patch = prompts.initial_prompt(crash_task(), context)
    assert "TEMPORARY" in morph and "reverted" in morph
    assert "DURABLE" in patch and "IndexError" in patch


def test_prompt_carries_the_schema_and_current_firmware():
    context = retriever.retrieve(heat_task(), BASELINE)
    prompt = prompts.initial_prompt(heat_task(), context)
    assert "Relay_Fan" in prompt and "forbidden_pins" in prompt
    assert "def sensor_loop" in prompt


# --- generation loop ---------------------------------------------------------


def test_happy_path_produces_a_verified_artifact():
    llm, sandbox = FakeLLM(good_replies()), FakeSandbox([passing()])
    result = Agent(llm, sandbox).run(heat_task(), BASELINE)

    assert not result.escalated
    assert result.output.plan.startswith("Enable Relay_Fan")
    assert result.output.pins_referenced == [17, 27]
    assert result.fw_hash
    assert len(result.attempts) == 1
    assert result.attempts[0].guardrail.status == "pass"
    # FR-12: the trace Guard Rail verified was captured at the tool, not claimed.
    assert {c.args["pin_number"] for c in result.output.tool_calls} == {17, 27}
    assert all(c.tool == TOOL_CHECK_HARDWARE_SCHEMA for c in result.output.tool_calls)


def test_guardrail_rejection_never_reaches_the_sandbox():
    """NFR-1 / SAFETY_PROTOCOL.md §1 layer 3: no code runs before Guard Rail."""
    forbidden = MORPH_CODE.replace("RelayFan(pin=27)", "RelayFan(pin=0)")
    llm = FakeLLM([
        FakeReply(tool_calls=[tool_call(17, "c1"), tool_call(0, "c2")]),
        code_reply("Use GPIO_0.", forbidden, [17, 0]),
        *good_replies(),
    ])
    sandbox = FakeSandbox([passing()])
    result = Agent(llm, sandbox).run(heat_task(), BASELINE)

    assert not result.escalated
    assert len(result.attempts) == 2
    assert result.attempts[0].guardrail.status == "fail"
    assert result.attempts[0].sandbox is None  # never executed
    assert len(sandbox.ran) == 1  # only the second, clean attempt


def test_sandbox_failure_feeds_results_and_delta_back():
    """FAIL(Results, ΔFirmware) reaches the Agent, not just "it broke"."""
    llm = FakeLLM([*good_replies(), *good_replies()])
    sandbox = FakeSandbox([failing(), passing()])
    result = Agent(llm, sandbox).run(crash_task(), BASELINE)

    assert not result.escalated
    assert len(result.attempts) == 2
    assert "IndexError" in llm.prompts_seen
    assert "ΔFirmware" in llm.prompts_seen


def test_retry_budget_is_shared_by_guardrail_and_sandbox():
    """SAFETY_PROTOCOL.md §4: one budget per event, both sources count."""
    forbidden = MORPH_CODE.replace("RelayFan(pin=27)", "RelayFan(pin=1)")
    llm = FakeLLM([
        FakeReply(tool_calls=[tool_call(17, "c1"), tool_call(1, "c2")]),
        code_reply("bad pin", forbidden, [17, 1]),
        *good_replies(),
        *good_replies(),
    ])
    sandbox = FakeSandbox([failing(), failing()])
    result = Agent(llm, sandbox).run(heat_task(), BASELINE)

    assert result.escalated
    assert len(result.attempts) == config.MAX_RETRIES
    assert result.output is None
    assert [a.guardrail.status for a in result.attempts] == ["fail", "pass", "pass"]


def test_max_retries_comes_from_config(monkeypatch):
    """CLAUDE.md §4: the retry cap is not a literal in the loop."""
    monkeypatch.setattr(config, "MAX_RETRIES", 1)
    llm, sandbox = FakeLLM(good_replies()), FakeSandbox([failing()])
    result = Agent(llm, sandbox).run(heat_task(), BASELINE)
    assert result.escalated
    assert len(result.attempts) == 1


def test_escalation_does_not_deploy_anything():
    """The escalated path must yield no artifact for deploy to pick up."""
    llm = FakeLLM([*good_replies(), *good_replies(), *good_replies()])
    sandbox = FakeSandbox([failing(), failing(), failing()])
    result = Agent(llm, sandbox).run(heat_task(), BASELINE)
    assert result.escalated and result.output is None and result.fw_hash is None


def test_malformed_model_output_burns_budget_instead_of_looping():
    """A model stuck emitting prose must terminate, not spin forever."""
    llm = FakeLLM([
        FakeReply(content="Sure! Here's what I'd do..."),
        FakeReply(content="```json\n{\"plan\": \"no code field\"}\n```"),
        *good_replies(),
    ])
    sandbox = FakeSandbox([passing()])
    result = Agent(llm, sandbox).run(heat_task(), BASELINE)
    assert not result.escalated
    assert len(result.attempts) == 1  # the two malformed turns produced no attempt
    assert "not valid JSON" in llm.prompts_seen or "missing code" in llm.prompts_seen


def test_tool_turn_cap_terminates_a_model_that_never_emits_code(monkeypatch):
    monkeypatch.setattr(config, "AGENT_MAX_TOOL_TURNS", 3)
    monkeypatch.setattr(config, "MAX_RETRIES", 1)
    llm = FakeLLM([FakeReply(tool_calls=[tool_call(27)]) for _ in range(3)])
    result = Agent(llm, FakeSandbox([])).run(heat_task(), BASELINE)
    assert result.escalated


def test_provenance_does_not_carry_between_attempts():
    """Attempt 2 must re-check its own pins; a stale trace proves nothing."""
    llm = FakeLLM([
        *good_replies(),
        # Attempts 2 and 3 emit code with no tool calls at all.
        code_reply("retry without checking", MORPH_CODE, [17, 27]),
        code_reply("retry without checking again", MORPH_CODE, [17, 27]),
    ])
    sandbox = FakeSandbox([failing()])
    result = Agent(llm, sandbox).run(heat_task(), BASELINE)
    assert result.escalated
    assert result.attempts[1].guardrail.status == "fail"
    assert "tool call" in result.attempts[1].guardrail.reason
    assert result.attempts[1].sandbox is None  # rejected before execution


def test_response_parsing_accepts_fenced_json():
    plan, code, pins = parse_response('```json\n{"plan": "p", "code": "x=1", "pins_referenced": [27]}\n```')
    assert (plan, code, pins) == ("p", "x=1", [27])


def test_response_parsing_requires_a_plan():
    """FR-13: no plan means no audit trail, so it is not a valid response."""
    with pytest.raises(GenerationError, match="plan"):
        parse_response('{"code": "x=1", "pins_referenced": []}')


def test_pipeline_imports_without_an_llm_configured(monkeypatch):
    """NFR-4: Guard Rail, Sandbox and rollback must work with no LLM SDK set up."""
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    from server.agent.agent import build_llm

    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        build_llm()
