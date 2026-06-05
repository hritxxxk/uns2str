import os
import json
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import StateGraph, START, END
from state import AgentState, MappingResponse, ReviewResponse
from agents import *

load_dotenv()
api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
os.environ["GOOGLE_API_KEY"] = api_key or ""

structured_llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash-lite").with_structured_output(MappingResponse)


def map_columns_node(state: AgentState) -> dict:
    if state["is_known_schema"]:
        return {}

    profile_text = json.dumps(state["profiles"][:MAX_PROFILE_COLS], indent=2)
    sample_text = json.dumps(state.get("sample_rows", [])[:3], indent=2)
    meta_text = json.dumps(state.get("metadata", []), indent=2)

    prompt = f"""Map each source column to a PIM attribute.

The PIM already has these defaults: sku_name, code, description, mrp, brand.
Map source columns TO them instead of recreating them.

target_attribute must be snake_case: "product_name", "item_code", "mrp", "brand".
attribute_type: Dropdown for brand/colour/size/gender/season/category, 
RichText for descriptions, Textbox for codes/names/numbers, 
MultiSelect for tags/features, Date for dates.
constraint: true ONLY for dropdown/multiselect.
mandatory: true ONLY for sku, code, product_name, mrp.

Source columns:
{profile_text}

Metadata:
{meta_text}

Sample rows:
{sample_text}"""

    response = structured_llm.invoke(prompt)
    mapped_cols = {m.source_column for m in response.mappings}

    # Pass 2: review unmapped columns — are they real attributes or noise?
    unmapped = [h for h in state.get("headers", []) if h not in mapped_cols]
    if unmapped:
        review_llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash-lite").with_structured_output(ReviewResponse)
        review_prompt = f"""Review these unmapped columns from a product catalog.
For each one, decide if it's a real product attribute worth keeping or just noise (internal notes, metadata, IDs, system fields).

Return is_valid_attribute=true only for actual product attributes (shipping info, materials, dimensions, features, etc.).
Return is_valid_attribute=false for system fields, audit timestamps, internal IDs, UDFs, boilerplate text.

Columns to review:
{json.dumps(unmapped, indent=2)}"""
        review_result = review_llm.invoke(review_prompt)
        for col in review_result.columns:
            if col.is_valid_attribute:
                response.mappings.append(ColumnMapping(
                    source_column=col.source_column,
                    target_attribute=col.target_attribute or col.source_column.lower().replace(" ", "_"),
                    attribute_type=col.attribute_type,
                    attribute_data_type="varchar",
                    constraint=False,
                    length=255,
                    mandatory=False,
                    attribute_group="Unclassified",
                    confidence=0.6
                ))

    confs = [m.confidence for m in response.mappings]
    avg_conf = sum(confs) / len(confs) if confs else 0

    save_cached_mapping(state["fingerprint"], [m.model_dump() for m in response.mappings])

    return {
        "mapping": response.mappings,
        "mapping_requires_review": avg_conf < 0.75
    }


def router(state: AgentState):
    if state["is_known_schema"]:
        return "step_attributes"
    return "step_map"


builder = StateGraph(AgentState)
builder.add_node("step_fingerprint", fingerprint_source)
builder.add_node("step_profile", profile_source)
builder.add_node("step_map", map_columns_node)
builder.add_node("step_attributes", build_attributes)
builder.add_node("step_references", collect_references)
builder.add_node("step_categories", resolve_category_paths)
builder.add_node("step_templates", fill_templates)

builder.add_edge(START, "step_fingerprint")
builder.add_edge("step_fingerprint", "step_profile")
builder.add_conditional_edges("step_profile", router, {
    "step_map": "step_map",
    "step_attributes": "step_attributes"
})
builder.add_edge("step_map", "step_attributes")
builder.add_edge("step_attributes", "step_references")
builder.add_edge("step_references", "step_categories")
builder.add_edge("step_categories", "step_templates")
builder.add_edge("step_templates", END)

graph = builder.compile()
