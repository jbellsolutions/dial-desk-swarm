from agency_swarm import Agent, ModelSettings
from openai.types.shared import Reasoning
from dotenv import load_dotenv

from config import get_default_model, is_openai_provider

load_dotenv()


def create_cto() -> Agent:
    return Agent(
        name="CTO",
        description=(
            "Chief Technology Officer for Dial Desk. Owns infrastructure, API integrations (GHL, Composio), "
            "deployments, tech stack decisions, and system reliability."
        ),
        instructions="./instructions.md",
        model=get_default_model(),
        model_settings=ModelSettings(
            reasoning=Reasoning(effort="medium", summary="auto") if is_openai_provider() else None,
        ),
    )


if __name__ == "__main__":
    from agency_swarm import Agency
    Agency(create_cto()).terminal_demo()
