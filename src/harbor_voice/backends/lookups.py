"""Read-only work-data lookups exposed to GitHub Copilot CLI as MCP servers.

Each source is a local MCP server the user already runs elsewhere (MSX, MSXi Snap, WorkIQ).
Only an explicit allow-list of read-only tools is made available to the model; anything that
writes, stages changes, or exposes personal compensation data is never offered.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LookupSource:
    name: str
    description: str
    command: str
    args: tuple[str, ...]
    tools: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)


def _home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home())


def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or _home() / "AppData" / "Local")


MSX_READ_TOOLS = (
    "crm_whoami",
    "crm_query",
    "crm_get_record",
    "list_opportunities",
    "get_my_active_opportunities",
    "get_milestones",
    "get_team_accounts",
    "list_accounts_by_tpid",
    "get_milestone_activities",
    "view_milestone_timeline",
    "view_opportunity_cost_trend",
    "get_opportunity_solutions",
    "get_opportunity_owner_roles",
    "find_opportunities_by_product",
    "list_territory_accounts",
    "list_territory_opportunities",
    "get_territory_overview",
    "pipeline_health",
    "stage_map",
)

MSXI_READ_TOOLS = (
    "msxi_search_accounts",
    "msxi_list_accounts",
    "msxi_get_account_overview",
    "msxi_get_account_review_bundle",
    "msxi_get_acr_summary",
    "msxi_get_acr_trend",
    "msxi_get_account_movers",
    "msxi_get_acr_grouped",
    "msxi_get_macc_kpis",
    "msxi_get_macc_contracts",
    "msxi_get_macc_pipeline",
    "msxi_get_milestones",
    "msxi_search_milestones",
    "msxi_get_milestone",
    "msxi_get_opportunity_correlations",
    "msxi_get_uat_actions",
    "msxi_search_uat",
    "msxi_get_factory",
    "msxi_get_nnr_summary",
    "msxi_get_billed_summary",
    "msxi_get_mw_seats",
    "msxi_get_region_capacity_constraints",
    "msxi_get_azure_health_action_items",
    "msxi_get_data_freshness",
)

WORKIQ_READ_TOOLS = ("ask", "retrieve")


def default_sources() -> dict[str, LookupSource]:
    home = _home()
    return {
        "msx": LookupSource(
            name="msx",
            description=(
                "live MSX CRM. For a customer's pipeline call list_opportunities with "
                "customerKeyword; get_milestones accepts customerKeyword too"
            ),
            command="node",
            args=(
                str(home / ".copilot" / "cache" / "MSX-IQ" / "mcp" / "msx" / "src" / "index.js"),
            ),
            tools=MSX_READ_TOOLS,
        ),
        "msxi": LookupSource(
            name="msxi",
            description="MSXi Snap snapshot: ACR, MACC, NNR, milestones, UAT, account overviews",
            command=str(
                _local_appdata() / "Programs" / "Microsoft" / "MSXi Snap" / "MSXi Snap.exe"
            ),
            args=("mcp", "--data-dir", str(_local_appdata() / "Microsoft" / "MSXi Snap")),
            tools=MSXI_READ_TOOLS,
        ),
        "workiq": LookupSource(
            name="workiq",
            description="Microsoft 365 via WorkIQ: email, meetings, chats, files, people",
            command=str(home / ".scout" / "bin" / "workiq.cmd"),
            args=("mcp",),
            tools=WORKIQ_READ_TOOLS,
        ),
    }


@dataclass(frozen=True, slots=True)
class LookupConfig:
    sources: tuple[LookupSource, ...]

    @classmethod
    def from_names(
        cls,
        names: list[str] | tuple[str, ...],
        catalogue: dict[str, LookupSource] | None = None,
    ) -> LookupConfig:
        catalogue = catalogue or default_sources()
        unknown = [name for name in names if name not in catalogue]
        if unknown:
            raise ValueError(f"unknown lookup source(s): {', '.join(unknown)}")
        return cls(tuple(catalogue[name] for name in dict.fromkeys(names)))

    def __bool__(self) -> bool:
        return bool(self.sources)

    def mcp_config(self) -> dict:
        servers = {}
        for source in self.sources:
            server = {
                "type": "local",
                "command": source.command,
                "args": list(source.args),
                "tools": list(source.tools),
            }
            if source.env:
                server["env"] = dict(source.env)
            servers[source.name] = server
        return {"mcpServers": servers}

    def available_tools(self) -> list[str]:
        return [f"{source.name}-{tool}" for source in self.sources for tool in source.tools]

    def allowed_tools(self) -> list[str]:
        return [f"{source.name}({tool})" for source in self.sources for tool in source.tools]

    def instructions(self) -> str:
        lines = "\n".join(f"- {source.name}: {source.description}" for source in self.sources)
        return (
            "Read-only work-data lookup tools are available. Use them only when the request "
            "needs the user's own work data, and prefer one focused call. Summarise results "
            "briefly in speech; never read out email bodies, personal data, or long lists.\n"
            f"{lines}\n"
        )
