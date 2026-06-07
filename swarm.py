import os
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

    from agents.orchestrator import create_orchestrator
    from agents.ceo import create_ceo
    from agents.cmo import create_cmo
    from agents.cto import create_cto
    from agents.coo import create_coo
    from agents.sales_director import create_sales_director
    from agents.builder import create_builder

    from agents.virtual_assistant import create_virtual_assistant
    from agents.deep_research import create_deep_research
    from agents.data_analyst import create_data_analyst
    from agents.slides_agent import create_slides_agent
    from agents.docs_agent import create_docs_agent
    from agents.video_generation_agent import create_video_generation_agent
    from agents.image_generation_agent import create_image_generation_agent

    orchestrator = create_orchestrator()
    ceo = create_ceo()
    cmo = create_cmo()
    cto = create_cto()
    coo = create_coo()
    sales_director = create_sales_director()
    builder = create_builder()

    virtual_assistant = create_virtual_assistant()
    deep_research = create_deep_research()
    data_analyst = create_data_analyst()
    slides_agent = create_slides_agent()
    docs_agent = create_docs_agent()
    video_generation_agent = create_video_generation_agent()
    image_generation_agent = create_image_generation_agent()

    executives = [ceo, cmo, cto, coo, sales_director, builder]
    specialists = [
        virtual_assistant,
        deep_research,
        data_analyst,
        slides_agent,
        docs_agent,
        video_generation_agent,
        image_generation_agent,
    ]

    all_agents = [orchestrator] + executives + specialists

    orchestrator_send_flows = [
        (orchestrator, agent, SendMessage)
        for agent in all_agents
        if agent is not orchestrator
    ]

    ceo_send_flows = [
        (ceo, agent, SendMessage)
        for agent in specialists
    ]

    handoff_flows = [
        (a > b, Handoff)
        for a in all_agents
        for b in all_agents
        if a is not b
    ]

    agency = Agency(
        *all_agents,
        communication_flows=orchestrator_send_flows + ceo_send_flows + handoff_flows,
        name="OpenSwarm",
        shared_instructions="shared_instructions.md",
        load_threads_callback=load_threads_callback,
    )

    return agency


if __name__ == "__main__":
    agency = create_agency()
    agency.tui(show_reasoning=True, reload=False)
