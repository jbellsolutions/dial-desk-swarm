from agency_swarm.tools import BaseTool
from pydantic import Field

from helpers import get_composio_tools


class FindTools(BaseTool):
    """
    Finds available Composio tools by toolkit names, specific tool names, or OAuth scopes.
    Use this when you know the toolkit or exact tool name you're looking for.
    For discovering tools by description, use SearchTools instead.
    If you already have the schema and the tool you need, DO NOT use this tool.
    """
    toolkits: list[str] = Field(
        default=[],
        description="List of toolkit names (e.g., ['GITHUB', 'GMAIL', 'SLACK']). "
                    "Required if tool_names is not provided."
    )
    tool_names: list[str] = Field(
        default=[],
        description="List of specific tool names (e.g., ['GITHUB_CREATE_ISSUE', 'GMAIL_SEND_EMAIL']). "
                    "Required if toolkits is not provided."
    )
    scopes: list[str] = Field(
        default=[],
        description="OAuth scopes to filter by permission level (e.g., ['write:org']). "
                    "Only works with a single toolkit."
    )
    limit: int = Field(
        default=10,
        description="Maximum number of tools to return. Default is 10."
    )
    include_args: bool = Field(
        default=False,
        description="If True, includes the arguments/parameters for each tool in the output. "
                    "Use ONLY when you are about to execute a specific tool. Do not set to true if you are still searching for the right tool."
    )

    def run(self):
        """Fetches and formats Composio tools based on the provided filters."""
        if self.tool_names:
            tools = get_composio_tools(tools=self.tool_names)
        elif self.toolkits:
            kwargs = {
                "toolkits": self.toolkits,
                "limit": self.limit,
            }
            if self.scopes:
                kwargs["scopes"] = self.scopes
            tools = get_composio_tools(**kwargs)
        else:
            return "Error: Provide either 'toolkits' or 'tool_names'."

        if isinstance(tools, dict) and tools.get("error"):
            return f"Error: {tools.get('error')}"

        return self._format_tools(tools)
    
    def _format_tools(self, tools: list) -> str:
        """Formats tools into a concise, token-efficient string."""
        if not tools:
            return "No tools found matching the criteria."
        
        formatted_lines = [f"Found {len(tools)} tool(s):\n"]
        
        for tool in tools:
            name = getattr(tool, 'name', 'Unknown')
            description = getattr(tool, 'description', 'No description')
            formatted_lines.append(f"• {name}: {description}")
            
            if self.include_args:
                args = self._extract_args(tool)
                if args:
                    formatted_lines.append(f"  Args: {args}")
        
        return "\n".join(formatted_lines)
    
    def _extract_args(self, tool) -> str:
        """Extracts and formats tool arguments as JSON."""
        import json
        try:
            params = getattr(tool, 'params_json_schema', None)
            if params and isinstance(params, dict) and 'properties' in params:
                return json.dumps(params, indent=2)
        except Exception:
            pass
        return ""


if __name__ == "__main__":
    # Test 1: Search by toolkit
    print("=== Test: Search by toolkit (GITHUB) ===")
    tool = FindTools(toolkits=["GITHUB"], limit=2)
    print(tool.run())
    print()
    
    # Test 2: Search by toolkit with args
    print("=== Test: Search by toolkit with arguments ===")
    tool = FindTools(toolkits=["GITHUB"], limit=2, include_args=True)
    print(tool.run())

