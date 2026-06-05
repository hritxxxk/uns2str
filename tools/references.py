from langchain_core.tools import tool


@tool
def extract_reference_values(mappings: list[dict], profiles: list[dict]) -> dict[str, list[str]]:
    """Extract unique values for Dropdown and MultiSelect attributes.
    
    Reads unique_values already collected during profiling — no extra file scan needed.
    Returns dict like {'Brand Master': ['Nike', 'Adidas'], 'Size Master': ['S', 'M', 'L']}."""
    refs = {}
    for m in mappings:
        attr_type = m.get("attribute_type", "") if isinstance(m, dict) else m.attribute_type
        if attr_type not in ("Dropdown", "MultiSelect"):
            continue
        src = m.get("source_column", "") if isinstance(m, dict) else m.source_column
        target = m.get("target_attribute", "") if isinstance(m, dict) else m.target_attribute
        profile = next((p for p in profiles if p["name"] == src), None)
        if profile and profile.get("unique_values"):
            master_key = f"{target.replace('_', ' ').title()} Master"
            refs[master_key] = sorted(profile["unique_values"])
    return refs
