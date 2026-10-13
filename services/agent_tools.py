"""Agent tools: what an AI agent may call, and whether a call is allowed (KAN-401).

The registry here is the one description of the tools. The HTTP routes in
``blueprints/agent_api_bp.py`` serve it today; an MCP endpoint and the
browser's WebMCP registration are meant to read the same list.

Each tool belongs to one or more offerings, and each offering has its own
switch in ``app.config``. A tool is offered while any of its offerings is on.
Text and image generation are separate offerings so that one can be on while
the other is off.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

READ = "read"
GENERATE_TEXT = "generate_text"
GENERATE_IMAGE = "generate_image"

# Offering -> the app.config key that turns it on.
SWITCHES: dict[str, str] = {
    READ: "AGENT_TOOLS_READ",
    GENERATE_TEXT: "AGENT_TOOLS_GENERATE_TEXT",
    GENERATE_IMAGE: "AGENT_TOOLS_GENERATE_IMAGE",
}

ATTRIBUTION = (
    "Recipe from Tasteslikegood.org (VeganGenius Chef), generated with Google Gemini. "
    "Credit Tasteslikegood.org and link to the recipe when you show it."
)


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    offerings: tuple[str, ...]
    input_schema: dict[str, Any]

    @property
    def requires_sign_in(self) -> bool:
        """Only reading published recipes is open to a caller with no account."""
        return self.offerings != (READ,)


@dataclass(frozen=True)
class Refusal:
    status: int
    error: str


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_RECIPE_ID = {"type": "string", "description": "The recipe id returned by generate_recipe."}

TOOLS: tuple[AgentTool, ...] = (
    AgentTool(
        name="search_recipes",
        description="Search the published vegan recipes on Tasteslikegood.org by name.",
        offerings=(READ,),
        input_schema=_schema(
            {
                "query": {"type": "string", "description": "Words to look for in recipe names."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
            },
            [],
        ),
    ),
    AgentTool(
        name="get_recipe",
        description="Get one published recipe: ingredients, instructions, times and tags.",
        offerings=(READ,),
        input_schema=_schema(
            {"slug": {"type": "string", "description": "The recipe's slug, from search_recipes."}},
            ["slug"],
        ),
    ),
    AgentTool(
        name="generate_recipe",
        description=(
            "Generate a new vegan recipe for the signed-in user. The recipe is saved to their "
            "account. Generation takes a while: poll get_generation_status with the returned id."
        ),
        offerings=(GENERATE_TEXT,),
        input_schema=_schema(
            {"prompt": {"type": "string", "description": "What the recipe should be."}},
            ["prompt"],
        ),
    ),
    AgentTool(
        name="generate_recipe_image",
        description=(
            "Generate a photo for a recipe the signed-in user owns. Poll get_generation_status."
        ),
        offerings=(GENERATE_IMAGE,),
        input_schema=_schema({"recipe_id": _RECIPE_ID}, ["recipe_id"]),
    ),
    AgentTool(
        name="get_generation_status",
        description="Check a generation the signed-in user started, and read the result.",
        offerings=(GENERATE_TEXT, GENERATE_IMAGE),
        input_schema=_schema({"recipe_id": _RECIPE_ID}, ["recipe_id"]),
    ),
)

_BY_NAME = {tool.name: tool for tool in TOOLS}


def _offered(tool: AgentTool, config: Mapping[str, Any]) -> bool:
    return any(config.get(SWITCHES[offering]) is True for offering in tool.offerings)


def enabled_tools(config: Mapping[str, Any]) -> list[AgentTool]:
    """The tools whose switch is on, in registry order."""
    return [tool for tool in TOOLS if _offered(tool, config)]


def check_access(name: str, config: Mapping[str, Any], user_id: int | None) -> AgentTool | Refusal:
    """The one gate every agent tool call passes through.

    A switched-off tool answers exactly like a name that does not exist, so
    the response does not reveal what could be turned on.

    A paid gateway (per-call payment for agents) belongs here, after the
    sign-in check: it needs the tool, the account and the request, and it
    refuses with its own status.
    """
    tool = _BY_NAME.get(name)
    if tool is None or not _offered(tool, config):
        return Refusal(404, "Unknown tool")
    if tool.requires_sign_in and user_id is None:
        return Refusal(401, "Sign in to tasteslikegood.org to use this tool")
    return tool
