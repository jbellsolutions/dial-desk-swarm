from agency_swarm import Agent, ModelSettings
from openai.types.shared import Reasoning
from dotenv import load_dotenv

from config import get_default_model, is_openai_provider

load_dotenv()


def create_coo() -> Agent:
    return Agent(
        name="COO",
        description=(
            "Chief Operating Officer for Dial Desk. Owns client onboarding, vendor management, "
            "delivery tracking, operations execution, and SOPs."
        ),
        instructions="./instructions.md",
        model=get_default_model(),
        model_settings=ModelSettings(
            reasoning=Reasoning(effort="medium", summary="auto") if is_openai_provider() else None,
        ),
    )


if __name__ == "__main__":
    from agency_swarm import Agency
    Agency(create_coo()).terminal_demo()
