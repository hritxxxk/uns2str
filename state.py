from typing import Optional
from langgraph.graph import MessagesState
from pydantic import BaseModel, Field


class ColumnMapping(BaseModel):
    source_column: str = Field(default="", description="Original column name from source file")
    target_attribute: str = Field(default="", description="Target PIM attribute name")
    attribute_type: str = Field(default="Textbox", description="Textbox, Dropdown, RichText, Textarea, MultiSelect, Date, Time")
    attribute_data_type: str = Field(default="varchar", description="varchar, int, float, boolean, date")
    constraint: bool = Field(default=False, description="True if dropdown or multiselect")
    length: Optional[int] = Field(None, description="Max field length")
    mandatory: bool = Field(default=False, description="True if this field is required")
    attribute_group: str = Field(default="Basic Information", description="Logical group name")
    confidence: float = Field(default=0.5, description="Confidence score 0.0 to 1.0")


class MappingResponse(BaseModel):
    mappings: list[ColumnMapping]


class AgentState(MessagesState):
    source_path: str
    sheet_name: Optional[str]
    fingerprint: str
    is_known_schema: bool
    headers: list[str]
    header_row: int
    data_start_row: int
    metadata: list[dict]
    profiles: list[dict]
    sample_rows: list[dict]
    row_count: int
    category_candidates: list[dict]
    category_path_config: dict
    category_hierarchy: list[str]
    mapping: list[ColumnMapping]
    mapping_requires_review: bool
    attribute_definitions: list[dict]
    reference_values: dict[str, list[str]]
    output_files: dict[str, str]
    error: Optional[str]
