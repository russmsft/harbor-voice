from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from harbor_voice.backends.copilot import CopilotAcpConnection, CopilotCliBackend
from harbor_voice.backends.lookups import (
    LookupConfig,
    LookupSource,
    default_sources,
)
from harbor_voice.domain import AssistantRequest, MessageResponse
from harbor_voice.meeting.agent import (
    LookupResult,
    MeetingAgent,
    MeetingAwareBackend,
    MeetingConfig,
)
from harbor_voice.meeting.transcript import PARTICIPANT
from tests.fakes import FakeBackend, FakeSpeaker
from tests.test_copilot_backend import FakeAcpFactory, ScriptedAcpProcess


def source(name: str, *tools: str) -> LookupSource:
    return LookupSource(
        name=name, description=f"{name} data", command="node", args=("x",), tools=tools
    )


# --- lookup configuration -----------------------------------------------------------------


def test_default_sources_offer_only_read_tools() -> None:
    sources = default_sources()
    forbidden = (
        "create",
        "update",
        "delete",
        "close",
        "execute",
        "reassign",
        "change_owner",
        "manage_",
        "earning",
        "mint",
        "payment",
        "do_action",
    )
    for item in sources.values():
        for tool in item.tools:
            assert not any(word in tool for word in forbidden), f"{item.name}:{tool}"
    assert set(sources) == {"msx", "msxi", "workiq"}


def test_lookup_config_builds_copilot_names_and_mcp_config() -> None:
    config = LookupConfig.from_names(
        ["msx", "msx"], {"msx": source("msx", "crm_whoami", "crm_query")}
    )

    assert config.available_tools() == ["msx-crm_whoami", "msx-crm_query"]
    assert config.allowed_tools() == ["msx(crm_whoami)", "msx(crm_query)"]
    server = config.mcp_config()["mcpServers"]["msx"]
    assert server["tools"] == ["crm_whoami", "crm_query"]
    assert "env" not in server  # an empty env would strip PATH and credentials
    assert "msx data" in config.instructions()


def test_lookup_config_rejects_unknown_sources_and_is_falsy_when_empty() -> None:
    with pytest.raises(ValueError):
        LookupConfig.from_names(["mail"])
    assert not LookupConfig.from_names([])


@pytest.mark.asyncio
async def test_backend_adds_lookup_servers_and_tools_to_cli_arguments(tmp_path: Path) -> None:
    fake = FakeAcpFactory()
    lookups = LookupConfig.from_names(["msx"], {"msx": source("msx", "crm_whoami")})
    provider = CopilotCliBackend(
        copilot_home=tmp_path / "home",
        executable="copilot",
        connection_factory=fake,
        lookups=lookups,
    )
    await provider.start(tmp_path)
    await provider.ask(AssistantRequest("who am I?"))

    arguments = fake.calls[0][0]
    config_path = Path(arguments[arguments.index("--additional-mcp-config") + 1][1:])
    assert json.loads(config_path.read_text())["mcpServers"]["msx"]["command"] == "node"
    assert arguments[arguments.index("--available-tools") + 1] == "view,grep,glob,msx-crm_whoami"
    assert arguments[arguments.index("--allow-tool") + 1] == "view,grep,glob,msx(crm_whoami)"
    assert "msx data" in fake.connection.prompts[0][1]
    assert provider.lookup_tools == frozenset({"msx-crm_whoami"})


@pytest.mark.asyncio
async def test_backend_without_lookups_keeps_minimal_tool_surface(tmp_path: Path) -> None:
    fake = FakeAcpFactory()
    provider = CopilotCliBackend(
        copilot_home=tmp_path / "home", executable="copilot", connection_factory=fake
    )
    await provider.start(tmp_path)
    await provider.ask(AssistantRequest("hi"))

    arguments = fake.calls[0][0]
    assert "--additional-mcp-config" not in arguments
    assert provider.lookup_tools == frozenset()


@pytest.mark.asyncio
async def test_acp_connection_reports_tool_calls(tmp_path: Path) -> None:
    process = ScriptedAcpProcess()

    async def factory(arguments, cwd, environment):
        return process

    connection = CopilotAcpConnection(["copilot"], tmp_path, {}, process_factory=factory)
    seen: list[str] = []
    connection.tool_call_listener = seen.append
    await connection.start()
    session = await connection.new_session(tmp_path)
    await connection.prompt(session, "hello")
    await connection.close()

    assert seen == ["Read a file"]


# --- meeting disclosure gate --------------------------------------------------------------


class ToolBackend(FakeBackend):
    """Fake inner backend that reports lookup tool usage like CopilotCliBackend."""

    def __init__(self, events, tools=(), *, lookup_tools=("msx-crm_query",)) -> None:
        super().__init__(events, MessageResponse(kind="message", message="Two open deals."))
        self.tools = list(tools)
        self.tool_listener = None
        self.last_tool_calls: list[str] = []
        self.lookup_tools = frozenset(lookup_tools)

    async def ask(self, request):
        self.last_tool_calls = []
        for tool in self.tools:
            self.last_tool_calls.append(tool)
            if self.tool_listener is not None:
                self.tool_listener(tool)
        return await super().ask(request)


@pytest.mark.asyncio
async def test_ask_tracked_filters_builtin_helpers() -> None:
    inner = ToolBackend([], ["tool_search", "msx-crm_query"])
    backend = MeetingAwareBackend(inner)
    heard: list[str] = []

    result = await backend.ask_tracked(AssistantRequest("q"), on_tool_call=heard.append)

    assert isinstance(result, LookupResult)
    assert result.tool_calls == ("msx-crm_query",)
    assert heard == ["msx-crm_query"]
    assert inner.tool_listener is None


def make_agent(tools, *, approval=True, timeout=5.0):
    events: list[str] = []
    speaker = FakeSpeaker(events)
    notices = []
    agent = MeetingAgent(
        backend=MeetingAwareBackend(ToolBackend(events, tools)),
        speaker=speaker,
        transcriber=None,
        config=MeetingConfig(
            owner_name="Russell", lookup_approval=approval, approval_timeout=timeout
        ),
        on_event=notices.append,
    )
    return agent, speaker, notices


@pytest.mark.asyncio
async def test_lookup_answer_waits_for_owner_approval() -> None:
    agent, speaker, notices = make_agent(["msx-crm_query"])

    task = asyncio.create_task(agent.respond("Scout, what's open?", asker=PARTICIPANT))
    for _ in range(50):
        if agent.awaiting_approval:
            break
        await asyncio.sleep(0.01)
    assert agent.awaiting_approval
    assert "Two open deals." not in speaker.spoken
    assert agent.resolve_approval(True)
    await task
    await agent.wait_idle()

    assert speaker.spoken == ["Let me check on that.", "Two open deals."]
    approval = next(n for n in notices if n.kind == "approval")
    assert approval.text == "Two open deals." and approval.question == "Scout, what's open?"


@pytest.mark.asyncio
async def test_declined_lookup_answer_is_withheld() -> None:
    agent, speaker, _ = make_agent(["msx-crm_query"])

    task = asyncio.create_task(agent.respond("Scout, what's open?", asker=PARTICIPANT))
    for _ in range(50):
        if agent.awaiting_approval:
            break
        await asyncio.sleep(0.01)
    agent.resolve_approval(False)
    await task
    await agent.wait_idle()

    assert "Two open deals." not in speaker.spoken
    assert "follow up" in speaker.spoken[-1]
    assert "[answer withheld]" in agent.transcript.render()


@pytest.mark.asyncio
async def test_unapproved_lookup_answer_times_out() -> None:
    agent, speaker, notices = make_agent(["msx-crm_query"], timeout=0.05)

    await agent.respond("Scout, what's open?", asker=PARTICIPANT)
    await agent.wait_idle()

    assert "Two open deals." not in speaker.spoken
    assert any("not approved in time" in n.text for n in notices)


@pytest.mark.asyncio
async def test_answers_without_lookups_or_with_approval_off_speak_immediately() -> None:
    agent, speaker, _ = make_agent([])
    await agent.respond("Scout, what's a VNet?", asker=PARTICIPANT)
    assert speaker.spoken == ["Two open deals."]

    agent, speaker, _ = make_agent(["msx-crm_query"], approval=False)
    await agent.respond("Scout, what's open?", asker=PARTICIPANT)
    await agent.wait_idle()
    assert speaker.spoken[-1] == "Two open deals."
