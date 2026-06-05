from langchain_core.tools import tool
from state import ColumnMapping, MappingResponse


@tool
def normalize_mapping(raw_list: list[dict]) -> list[dict]:
    """Clean up field names from LLM output.
    
    Handles variations like 'constrained' → 'constraint', 
    'target' → 'target_attribute', 'type' → 'attribute_type'."""
    result = []
    for m in raw_list:
        src = m.get("source_column", "")
        target = m.pop("target", m.get("target_attribute", src))
        target = target.lower().replace(" ", "_").replace("-", "_")
        m["target_attribute"] = target
        m["constraint"] = m.pop("constrained", m.get("constraint", False))
        m["attribute_type"] = m.pop("type", m.get("attribute_type", "Textbox"))
        m["attribute_data_type"] = m.pop("data_type", m.get("attribute_data_type", "varchar"))
        m["attribute_group"] = m.pop("group", m.get("attribute_group", "Basic Information"))
        result.append(m)
    return result


@tool
def validate_mapping(raw_list: list[dict]) -> list[ColumnMapping]:
    """Validate mapping dicts against the Pydantic schema.
    
    Returns ColumnMapping objects with all fields verified."""
    parsed = MappingResponse(mappings=[ColumnMapping(**m) for m in raw_list])
    return parsed.mappings


@tool
def build_attribute_definitions(mappings: list[ColumnMapping]) -> list[dict]:
    """Convert column mappings into 17-column PIM attribute definitions.
    
    Applies length rules: RichText=65536, Textarea=16384, image=2048, else 255.
    Sets constraint=True for Dropdown/MultiSelect.
    Builds reference_master and reference_attribute for constrained fields."""
    defs = []
    for m in mappings:
        a_type = m.attribute_type
        constraint = m.constraint or a_type in ("Dropdown", "MultiSelect", "MultiSelectDropdown", "MultiTextBox")

        if a_type == "RichText":
            length = 65536
        elif a_type == "Textarea":
            length = 16384
        elif "image" in m.target_attribute.lower():
            length = 2048
        else:
            length = m.length or 255

        ref_master = f"{m.target_attribute.replace('_', ' ').title()} Master" if constraint else ""
        ref_attr = m.target_attribute.lower().replace(" ", "_") if constraint else ""

        defs.append({
            "attribute_name": m.target_attribute.lower().replace(" ", "_"),
            "short_name": m.target_attribute.lower().replace(" ", "_"),
            "display_name": m.target_attribute.replace("_", " ").title(),
            "attribute_type": a_type,
            "attribute_data_type": m.attribute_data_type,
            "constraint": constraint,
            "length": length,
            "mandatory": m.mandatory,
            "filter": True,
            "editability": True,
            "visibility": True,
            "searchable": True,
            "auto_translate": False,
            "attribute_group": m.attribute_group,
            "reference_master": ref_master,
            "reference_attribute": ref_attr,
            "status": "Active"
        })
    return defs
