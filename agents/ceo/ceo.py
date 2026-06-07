from agency_swarm import Agent, ModelSettings
from openai.types.shared import Reasoning
from dotenv import load_dotenv

from config import get_default_model, is_openai_provider

load_dotenv()


def create_ceo() -> Agent:
    return Agent(
        name="CEO",
        description=(
            "Strategic leader of Dial Desk / AI Integraterz. Monitors KPIs, makes decisions, "
            "delegates to CMO/CTO/COO/Sales Director, and approves spend under $500."
        ),
        instructions="./instructions.md",
        model=get_default_model(),
        model_settings=ModelSettings(
            reasoning=Reasoning(effort="medium", summary="auto") if is_openai_provider() else None,
        ),
    )


if __name__ == "__main__":
    from agency_swarm import Agency
    Agency(create_ceo()).terminal_demo()
