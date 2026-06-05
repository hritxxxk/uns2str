from langgraph.graph import StateGraph, START, END
from state import AgentState
from agents import *


def router(state: AgentState):
    if state["is_known_schema"]:
        return "step_attributes"
    return "step_map"


graph = (
    StateGraph(AgentState)
    .add_node("step_fingerprint", fingerprint_source)
    .add_node("step_profile", profile_source)
    .add_node("step_map", map_columns)
    .add_node("step_attributes", build_attributes)
    .add_node("step_references", collect_references)
    .add_node("step_categories", resolve_category_paths)
    .add_node("step_templates", fill_templates)
    .add_edge(START, "step_fingerprint")
    .add_edge("step_fingerprint", "step_profile")
    .add_conditional_edges("step_profile", router, {
        "step_map": "step_map",
        "step_attributes": "step_attributes"
    })
    .add_edge("step_map", "step_attributes")
    .add_edge("step_attributes", "step_references")
    .add_edge("step_references", "step_categories")
    .add_edge("step_categories", "step_templates")
    .add_edge("step_templates", END)
    .compile()
)
