from agency_swarm.tools import BaseTool
from pydantic import Field

from helpers import execute_composio_tool


class SearchTools(BaseTool):
    """
    Search Composio tools for a task and return recommended tools + schemas.

    Use this when you need to discover tools for a new external workflow.
    """

    queries: list[dict] = Field(
        ...,
        description=(
            "List of query objects for COMPOSIO_SEARCH_TOOLS. "
            "Each item should usually include at least a 'use_case' key."
        ),
    )
    session: dict | None = Field(
        default=None,
        description=(
            "Optional Composio search session payload. "
            "Example: {'generate_id': true} or {'id': 'existing-session-id'}."
        ),
    )
    model: str | None = Field(
        default=None,
        description="Optional model hint to pass through to COMPOSIO_SEARCH_TOOLS.",
    )

    def run(self):
        arguments: dict = {"queries": self.queries}
        if self.session is not None:
            arguments["session"] = self.session
        if self.model:
            arguments["model"] = self.model

        result = execute_composio_tool(
            tool_name="COMPOSIO_SEARCH_TOOLS",
            arguments=arguments,
        )
        return str(result)
