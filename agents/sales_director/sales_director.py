from agency_swarm import Agent, ModelSettings
from openai.types.shared import Reasoning
from dotenv import load_dotenv

from config import get_default_model, is_openai_provider

load_dotenv()


def create_sales_director() -> Agent:
    return Agent(
        name="Sales Director",
        description=(
            "Sales Director for Dial Desk. Owns SMS/email outreach, lead qualification, "
            "pipeline management, deal closing, and dialer operations."
        ),
        instructions="./instructions.md",
        model=get_default_model(),
        model_settings=ModelSettings(
            reasoning=Reasoning(effort="medium", summary="auto") if is_openai_provider() else None,
        ),
    )


if __name__ == "__main__":
    from agency_swarm import Agency
    Agency(create_sales_director()).terminal_demo()
