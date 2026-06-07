from agency_swarm import Agent, ModelSettings
from openai.types.shared import Reasoning
from dotenv import load_dotenv

from config import get_default_model, is_openai_provider
from shared_tools.builder_tools import CreateAgent, RegisterAgent, CreateSwarm, RestartSwarm

load_dotenv()


def create_builder() -> Agent:
    return Agent(
        name="Builder",
        description=(
            "Meta-engineer that creates agents, registers them into swarms, "
            "scaffolds new swarms, and restarts servers to apply changes."
        ),
        instructions="./instructions.md",
        model=get_default_model(),
        model_settings=ModelSettings(
            reasoning=Reasoning(effort="medium", summary="auto") if is_openai_provider() else None,
        ),
        tools=[
            CreateAgent,
            RegisterAgent,
            CreateSwarm,
            RestartSwarm,
        ],
    )


if __name__ == "__main__":
    from agency_swarm import Agency
    Agency(create_builder()).terminal_demo()
