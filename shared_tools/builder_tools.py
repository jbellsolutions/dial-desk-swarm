"""Builder tools — create agents, register them, scaffold swarms, and restart the server."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from agency_swarm.tools import BaseTool
from pydantic import Field


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_AGENT_PY_TEMPLATE = '''from agency_swarm import Agent, ModelSettings
from openai.types.shared import Reasoning
from dotenv import load_dotenv

from config import get_default_model, is_openai_provider

load_dotenv()


def create_{name}() -> Agent:
    return Agent(
        name="{DisplayName}",
        description="{description}",
        instructions="./instructions.md",
        model=get_default_model(),
        model_settings=ModelSettings(
            reasoning=Reasoning(effort="medium", summary="auto") if is_openai_provider() else None,
        ),
    )


if __name__ == "__main__":
    from agency_swarm import Agency
    Agency(create_{name}()).terminal_demo()
'''

_INIT_PY_TEMPLATE = "from .{name} import create_{name}\n"

_SWARM_PY_TEMPLATE = '''import os
from dotenv import load_dotenv
from agents import set_tracing_disabled, set_tracing_export_api_key
from patches.patch_agency_swarm_dual_comms import apply_dual_comms_patch
from patches.patch_file_attachment_refs import apply_file_attachment_reference_patch
from patches.patch_ipython_interpreter_composio import apply_ipython_composio_context_patch
from patches.patch_utf8_file_reads import apply_utf8_file_read_patch

load_dotenv()

apply_utf8_file_read_patch()
apply_dual_comms_patch()
apply_file_attachment_reference_patch()
apply_ipython_composio_context_patch()

_tracing_key = os.getenv("OPENAI_API_KEY")
if _tracing_key and _tracing_key.startswith("sk-"):
    set_tracing_export_api_key(_tracing_key)
else:
    set_tracing_disabled(True)


def create_agency(load_threads_callback=None):
    from agency_swarm import Agency
    from agency_swarm.tools import Handoff, SendMessage

    from orchestrator import create_orchestrator

    orchestrator = create_orchestrator()

    all_agents = [orchestrator]

    orchestrator_send_flows = [
        (orchestrator, agent, SendMessage)
        for agent in all_agents
        if agent is not orchestrator
    ]

    handoff_flows = [
        (a > b, Handoff)
        for a in all_agents
        for b in all_agents
        if a is not b
    ]

    agency = Agency(
        *all_agents,
        communication_flows=orchestrator_send_flows + handoff_flows,
        name="OpenSwarm",
        shared_instructions="shared_instructions.md",
        load_threads_callback=load_threads_callback,
    )

    return agency


if __name__ == "__main__":
    agency = create_agency()
    agency.tui(show_reasoning=True, reload=False)
'''

_SERVER_PY_TEMPLATE = '''# FastAPI entry point — run with: python server.py

import logging
from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)

from swarm import create_agency
from agency_swarm.integrations.fastapi import run_fastapi


if __name__ == "__main__":
    run_fastapi(
        agencies={{
            "open-swarm": create_agency,
        }},
        port=8080,
        enable_logging=True,
        allowed_local_file_dirs=[
            "./uploads",
        ],
    )
'''

_SHARED_INSTRUCTIONS_TEMPLATE = '''# Shared Instructions

All agents in this swarm share the following context and rules.

## Context
- This is an OpenSwarm agency.
- Be direct, results-oriented, and concise.
- When unsure, say "I don't know" — never guess.
- Lead with outcomes, not reasoning.
'''

_ORCHESTRATOR_PY_TEMPLATE = '''from agency_swarm import Agent, ModelSettings
from openai.types.shared import Reasoning
from dotenv import load_dotenv

from config import get_default_model, is_openai_provider

load_dotenv()


def create_orchestrator() -> Agent:
    return Agent(
        name="Orchestrator",
        description="Routes incoming tasks to the correct specialist.",
        instructions="./instructions.md",
        model=get_default_model(),
        model_settings=ModelSettings(
            reasoning=Reasoning(effort="medium", summary="auto") if is_openai_provider() else None,
        ),
    )


if __name__ == "__main__":
    from agency_swarm import Agency
    Agency(create_orchestrator()).terminal_demo()
'''

_ORCHESTRATOR_INSTRUCTIONS_TEMPLATE = '''# Orchestrator Instructions

You are the **Orchestrator** for this swarm.

## Role
Route tasks to the correct specialist. Do not perform specialist work yourself.

## Communication Style
- Direct, results-oriented
- Short answers, no architecture lectures
'''

_CONFIG_PY_TEMPLATE = '''"""Shared model configuration helpers — read by all agents at startup."""
import os


def get_default_model(fallback: str = "gpt-5.2"):
    """Return the configured default model for standard agents."""
    model = os.getenv("DEFAULT_MODEL", fallback)
    return _resolve(model)


def is_openai_provider() -> bool:
    """Return True when the configured provider is OpenAI (not LiteLLM)."""
    return "/" not in os.getenv("DEFAULT_MODEL", "")


def _resolve(model: str):
    if "/" not in model:
        return model
    bare = model[len("litellm/"):] if model.startswith("litellm/") else model
    try:
        from agency_swarm import LitellmModel
        return LitellmModel(model=bare)
    except ImportError:
        return model
'''


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snake(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name.lower().strip()).strip("_")


def _capitalize(name: str) -> str:
    return " ".join(w.capitalize() for w in name.replace("_", " ").split())


def _swarm_root() -> Path:
    """Return the root of the current swarm (where server.py lives)."""
    # Try current working directory; fall back to script location
    cwd = Path.cwd()
    if (cwd / "server.py").is_file():
        return cwd
    # If inside an agent/tools dir, walk up to find server.py
    for p in Path(__file__).parents:
        if (p / "server.py").is_file():
            return p
    return cwd


def _is_repo_style(root: Path) -> bool:
    """True if the swarm uses package-style imports (agents/ceo/)."""
    return (root / "agents" / "__init__.py").is_file()


# ---------------------------------------------------------------------------
# Tool: CreateAgent
# ---------------------------------------------------------------------------

class CreateAgent(BaseTool):
    """
    Create a new agent package with boilerplate files:
    __init__.py, <name>.py, instructions.md, and a tools/ stub.
    The agent is NOT yet registered in swarm.py — call RegisterAgent after this.
    """

    agent_name: str = Field(
        ...,
        description="Snake-case name for the agent (e.g. 'seo_auditor').",
    )
    display_name: Optional[str] = Field(
        default=None,
        description="Pretty display name. Defaults to title-cased agent_name.",
    )
    description: str = Field(
        ...,
        description="One-sentence description of the agent's purpose.",
    )
    instructions: str = Field(
        ...,
        description="The agent's system prompt (markdown). Be specific and direct.",
    )
    tools: Optional[str] = Field(
        default=None,
        description="Comma-separated list of tool class names this agent will need (optional).",
    )

    def run(self) -> str:
        root = _swarm_root()
        name = _snake(self.agent_name)
        display = self.display_name or _capitalize(name)
        desc = self.description.strip()
        instr = self.instructions.strip()
        is_repo = _is_repo_style(root)

        if is_repo:
            agent_dir = root / "agents" / name
        else:
            agent_dir = root / name

        if agent_dir.exists():
            return f"ERROR: Agent directory already exists: {agent_dir}"

        agent_dir.mkdir(parents=True)
        (agent_dir / "tools").mkdir()

        # __init__.py
        (agent_dir / "__init__.py").write_text(_INIT_PY_TEMPLATE.format(name=name))

        # <name>.py
        agent_py = agent_dir / f"{name}.py"
        agent_py.write_text(
            _AGENT_PY_TEMPLATE.format(
                name=name,
                DisplayName=display,
                description=desc.replace('"', '\\"'),
            )
        )

        # instructions.md
        (agent_dir / "instructions.md").write_text(f"# {display} Instructions\n\n{instr}\n")

        # tools/__init__.py
        (agent_dir / "tools" / "__init__.py").write_text("\n")

        # Optional tool stubs
        if self.tools:
            for tool_name in [t.strip() for t in self.tools.split(",") if t.strip()]:
                stub = agent_dir / "tools" / f"{tool_name}.py"
                stub.write_text(
                    "from agency_swarm.tools import BaseTool\n"
                    "from pydantic import Field\n\n"
                    f"class {tool_name}(BaseTool):\n"
                    f'    """TODO: Implement {tool_name}."""\n\n'
                    "    def run(self) -> str:\n"
                    '        return "Not implemented yet."\n'
                )

        extra = " (package-style)" if is_repo else " (flat-style)"
        return (
            f"Created agent '{display}' at {agent_dir}{extra}\n"
            "Next step: call RegisterAgent to wire it into swarm.py, then RestartSwarm."
        )


# ---------------------------------------------------------------------------
# Tool: RegisterAgent
# ---------------------------------------------------------------------------

class RegisterAgent(BaseTool):
    """
    Register an existing agent into swarm.py by adding its import,
    instantiation, and list membership. Does NOT restart the server.
    """

    agent_name: str = Field(
        ...,
        description="Snake-case name of the agent to register (must match its directory).",
    )
    agent_group: str = Field(
        default="specialists",
        description="Which group in swarm.py: 'executives' (C-suite) or 'specialists' (production).",
    )

    def run(self) -> str:
        root = _swarm_root()
        name = _snake(self.agent_name)
        group = self.agent_group.lower().strip()
        if group not in ("executives", "specialists"):
            return f"ERROR: agent_group must be 'executives' or 'specialists', got '{group}'"

        swarm_py = root / "swarm.py"
        if not swarm_py.is_file():
            return f"ERROR: swarm.py not found at {swarm_py}"

        lines = swarm_py.read_text().splitlines(keepends=True)
        text = "".join(lines)

        is_repo = _is_repo_style(root)
        import_line = f"from {'agents.' if is_repo else ''}{name} import create_{name}"

        # Prevent double-register
        if f"create_{name}()" in text:
            return f"Agent '{name}' already registered in swarm.py. No changes made."

        # ------------------------------------------------------------------
        # 1. Insert import after the line that imports Handoff, SendMessage
        # ------------------------------------------------------------------
        import_inserted = False
        for i, line in enumerate(lines):
            if "from agency_swarm.tools import" in line:
                # Keep the same indent (usually 4 spaces)
                indent = line[: len(line) - len(line.lstrip())]
                lines.insert(i + 1, f"{indent}{import_line}\n")
                import_inserted = True
                break
        if not import_inserted:
            return "ERROR: Could not find 'from agency_swarm.tools import' insertion point."

        # ------------------------------------------------------------------
        # 2. Insert instantiation after the last create_x() in the group
        # ------------------------------------------------------------------
        # Strategy: find the block of variable assignments that belong to the group,
        # then append after the last one.
        # In standard swarms every group is defined via a separate list, so we look
        # for the LAST line matching "    <something> = create_<something>()" that
        # appears before the group list definition.
        create_inserted = False
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i]
            # Find a line like "    image_generation_agent = create_image_generation_agent()"
            if re.match(r"^    \w+ = create_\w+\(\)\n", line):
                # Make sure it belongs to our target group by checking what
                # variable list comes next. We search forward from this line
                # for a list definition matching our group.
                for j in range(i + 1, len(lines)):
                    if re.match(rf"^    {group} = \[", lines[j]):
                        lines.insert(i + 1, f"    {name} = create_{name}()\n")
                        create_inserted = True
                        break
                if create_inserted:
                    break
                # Also check if this line appears before the generic all_agents list
                for j in range(i + 1, len(lines)):
                    if re.match(r"^    all_agents = \[", lines[j]):
                        lines.insert(i + 1, f"    {name} = create_{name}()\n")
                        create_inserted = True
                        break
                if create_inserted:
                    break
        if not create_inserted:
            return "ERROR: Could not find instantiation insertion point."

        # ------------------------------------------------------------------
        # 3. Insert "        <name>," into the group list
        # ------------------------------------------------------------------
        list_inserted = False
        for i, line in enumerate(lines):
            if re.match(rf"^    {group} = \[", line):
                # The list block starts here. Find the closing "    ]".
                for j in range(i + 1, len(lines)):
                    if lines[j].strip() == "]":
                        # Figure out the item indent (usually 8 spaces)
                        list_indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
                        item_indent = "        " if len(list_indent) == 4 else list_indent + "    "
                        lines.insert(j, f"{item_indent}{name},\n")
                        list_inserted = True
                        break
                if list_inserted:
                    break
        if not list_inserted:
            # Try the flat all_agents list
            for i, line in enumerate(lines):
                if re.match(r"^    all_agents = \[", line):
                    for j in range(i + 1, len(lines)):
                        if lines[j].strip() == "]":
                            item_indent = "        "
                            lines.insert(j, f"{item_indent}{name},\n")
                            list_inserted = True
                            break
                    if list_inserted:
                        break
        if not list_inserted:
            return "ERROR: Could not find list insertion point."

        swarm_py.write_text("".join(lines))
        return (
            f"Registered '{name}' in swarm.py under '{group}'.\n"
            "Run RestartSwarm to activate."
        )


# ---------------------------------------------------------------------------
# Tool: CreateSwarm
# ---------------------------------------------------------------------------

class CreateSwarm(BaseTool):
    """
    Scaffold a brand-new swarm directory from scratch.
    Creates server.py, swarm.py, config.py, shared_instructions.md,
    patches/, shared_tools/, and an orchestrator agent.
    """

    swarm_name: str = Field(
        ...,
        description="Snake-case name for the new swarm (e.g. 'credit_repair_swarm').",
    )
    base_path: Optional[str] = Field(
        default=None,
        description="Directory where the swarm will be created. Defaults to ~/.agent-os/swarms/<name>",
    )
    port: int = Field(
        default=8080,
        description="Port for the new swarm server. Default 8080 (change if running side-by-side).",
    )

    def run(self) -> str:
        name = _snake(self.swarm_name)
        if self.base_path:
            root = Path(self.base_path).expanduser().resolve() / name
        else:
            root = Path.home() / ".agent-os" / "swarms" / name

        if root.exists():
            return f"ERROR: Directory already exists: {root}"

        root.mkdir(parents=True)
        (root / "patches").mkdir()
        (root / "shared_tools").mkdir()
        (root / "orchestrator").mkdir()
        (root / "orchestrator" / "tools").mkdir()

        # config.py
        (root / "config.py").write_text(_CONFIG_PY_TEMPLATE)

        # server.py
        server_py = _SERVER_PY_TEMPLATE.replace("port=8080", f"port={self.port}")
        (root / "server.py").write_text(server_py)

        # swarm.py
        (root / "swarm.py").write_text(_SWARM_PY_TEMPLATE)

        # shared_instructions.md
        (root / "shared_instructions.md").write_text(_SHARED_INSTRUCTIONS_TEMPLATE)

        # orchestrator
        (root / "orchestrator" / "__init__.py").write_text(
            "from .orchestrator import create_orchestrator\n"
        )
        (root / "orchestrator" / "orchestrator.py").write_text(_ORCHESTRATOR_PY_TEMPLATE)
        (root / "orchestrator" / "instructions.md").write_text(
            _ORCHESTRATOR_INSTRUCTIONS_TEMPLATE
        )
        (root / "orchestrator" / "tools" / "__init__.py").write_text("\n")

        # .env.example
        (root / ".env.example").write_text(
            "OPENAI_API_KEY=\n"
            "DEFAULT_MODEL=gpt-5.2\n"
        )

        # .gitignore
        (root / ".gitignore").write_text(
            ".env\n"
            "__pycache__/\n"
            "*.pyc\n"
            ".venv/\n"
            "uploads/\n"
            "activity-logs/\n"
            "mnt/\n"
        )

        return (
            f"Created new swarm '{name}' at {root}\n"
            f"Port: {self.port}\n"
            "To run:\n"
            f"  cd {root} && python -m venv .venv && source .venv/bin/activate\n"
            f"  pip install agency-swarm python-dotenv openai litellm\n"
            f"  cp .env.example .env   # fill in your keys\n"
            f"  python server.py\n"
            "To add agents, run Builder tools inside this swarm."
        )


# ---------------------------------------------------------------------------
# Tool: RestartSwarm
# ---------------------------------------------------------------------------

class RestartSwarm(BaseTool):
    """
    Gracefully restart the server process so new agents and swarm.py changes
    take effect. The restart is deferred ~3 seconds so the tool call can return first.
    """

    wait_seconds: int = Field(
        default=3,
        description="Seconds to wait before killing the old process.",
    )

    def run(self) -> str:
        root = _swarm_root()

        # Find PIDs for python server.py and port 8080
        pids = set()

        result = subprocess.run(
            ["pgrep", "-f", "python server.py"],
            capture_output=True, text=True,
        )
        for pid in result.stdout.strip().split("\n"):
            if pid.strip().isdigit():
                pids.add(int(pid.strip()))

        port_result = subprocess.run(
            ["lsof", "-t", "-i:8080"],
            capture_output=True, text=True,
        )
        for pid in port_result.stdout.strip().split("\n"):
            if pid.strip().isdigit():
                pids.add(int(pid.strip()))

        if not pids:
            return (
                "WARNING: Could not find a running server process (no PID on port 8080).\n"
                f"You can start it manually: cd {root} && python server.py"
            )

        # Write a deferred restart script
        script = f"""#!/bin/bash
sleep {self.wait_seconds}
kill {' '.join(str(p) for p in pids)} 2>/dev/null
sleep 1
cd {root}
nohup {sys.executable} server.py > /tmp/swarm_{self.wait_seconds}.log 2>&1 &
"""
        script_path = Path("/tmp") / f"restart_swarm_{self.wait_seconds}.sh"
        script_path.write_text(script)
        script_path.chmod(0o755)

        # Spawn it in background so this tool returns before the kill
        subprocess.Popen(
            ["bash", str(script_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        return (
            f"Restart scheduled in {self.wait_seconds}s. PIDs to kill: {sorted(pids)}.\n"
            "The API will briefly go down (~5s) and come back on port 8080."
        )
