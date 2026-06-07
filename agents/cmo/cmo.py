from agency_swarm import Agent, ModelSettings
from openai.types.shared import Reasoning
from dotenv import load_dotenv

from config import get_default_model, is_openai_provider

load_dotenv()


def create_cmo() -> Agent:
    return Agent(
        name="CMO",
        description=(
            "Chief Marketing Officer for Dial Desk. Owns campaigns, landing pages, ad management, copy, "
            "lead program management, and brand positioning for MCA lead gen."
        ),
        instructions="./instructions.md",
        model=get_default_model(),
        model_settings=ModelSettings(
            reasoning=Reasoning(effort="medium", summary="auto") if is_openai_provider() else None,
        ),
    )


if __name__ == "__main__":
    from agency_swarm import Agency
    Agency(create_cmo()).terminal_demo()
