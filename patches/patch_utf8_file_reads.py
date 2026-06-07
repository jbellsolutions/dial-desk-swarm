"""Force UTF-8 for Agency Swarm instruction file reads on Windows.

Agency Swarm currently reads agent and shared instruction files with the
platform default encoding. On Windows that can be cp1252, which fails on the
UTF-8 markdown shipped with OpenSwarm before the local TUI bridge starts.
"""

from __future__ import annotations

import os


def apply_utf8_file_read_patch() -> None:
    try:
        from agency_swarm.agency import core as agency_core
        from agency_swarm.agency import helpers as agency_helpers
        from agency_swarm.agent.file_manager import AgentFileManager
    except Exception:
        return

    if getattr(AgentFileManager.read_instructions, "_openswarm_utf8_patched", False):
        return

    def read_shared_instructions(agency, path: str) -> None:
        with open(path, encoding="utf-8") as f:
            agency.shared_instructions = f.read()

    def read_agent_instructions(self) -> None:
        if not self.agent.instructions:
            return
        if not isinstance(self.agent.instructions, str):
            return

        class_instructions_path = os.path.normpath(
            os.path.join(self.get_class_folder_path(), self.agent.instructions)
        )
        if os.path.isfile(class_instructions_path):
            with open(class_instructions_path, encoding="utf-8") as f:
                self.agent.instructions = f.read()
        elif os.path.isfile(self.agent.instructions):
            with open(self.agent.instructions, encoding="utf-8") as f:
                self.agent.instructions = f.read()

    read_agent_instructions._openswarm_utf8_patched = True  # type: ignore[attr-defined]

    agency_helpers.read_instructions = read_shared_instructions
    agency_core.read_instructions = read_shared_instructions
    AgentFileManager.read_instructions = read_agent_instructions
