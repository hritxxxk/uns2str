from typing import Annotated, Optional

from langgraph.channels.delta import DeltaChannel
from langgraph.graph import MessagesState
from langgraph.graph.message import _messages_delta_reducer
from pydantic import BaseModel, Field


class ColumnMapping(BaseModel):
    source_column: str = Field(default="", description="Original column name from source file")
    target_attribute: str = Field(default="", description="Target PIM attribute name")
    attribute_type: str = Field(default="Textbox", description="Textbox, Dropdown, RichText, Textarea, MultiSelect, MultiSelectDropdown, MultiTextBox, Date, Time")
    attribute_data_type: str = Field(default="varchar", description="varchar, varchar[], int, float, boolean, date")
    constraint: bool = Field(default=False, description="True if dropdown, multiselect, multiselectdropdown, or multitextbox")
    length: Optional[int] = Field(None, description="Max field length")
    mandatory: bool = Field(default=False, description="True if this field is required")
    attribute_group: str = Field(default="Basic Information", description="Logical group name")
    confidence: float = Field(default=0.5, description="Confidence score 0.0 to 1.0")


PIM_DEFAULTS = ["sku_name", "code", "mrp"]


class ColumnReview(BaseModel):
    source_column: str = Field(default="", description="Column name")
    is_valid_attribute: bool = Field(default=False, description="True if this is a real product attribute worth keeping")
    target_attribute: str = Field(default="", description="PIM attribute name if valid")
    attribute_type: str = Field(default="Textbox", description="Textbox, Dropdown, etc.")
    reason: str = Field(default="", description="Why this was flagged or kept")


class ReviewResponse(BaseModel):
    columns: list[ColumnReview]


class CategoryValidation(BaseModel):
    is_valid: bool = Field(description="Whether the candidate paths form a valid category hierarchy")
    reason: str = Field(default="", description="If invalid, why. If valid, confirmation")


class MappingResponse(BaseModel):
    mappings: list[ColumnMapping]


class MappingLLMResponse(BaseModel):

    mapping: list[ColumnMapping] = Field(default=[], description="Inferred column-to-PIM-attribute mappings")
    core_sku_column: str = Field(default="", description="Source column name that holds the SKU")
    core_code_column: str = Field(default="", description="Source column name that holds the product code")
    core_mrp_column: str = Field(default="", description="Source column name that holds the MRP/price")
    core_category_column: str = Field(default="", description="Source column name that holds the category path")
    needs_human_input: bool = Field(default=False, description="True when avg confidence < 0.7 or core column unclear")


class IngestionOutput(BaseModel):
    status: str = Field(description="success or partial or failed")
    fingerprint: str = Field(default="", description="File fingerprint")
    attribute_count: int = Field(default=0, description="Number of attributes created")
    reference_count: int = Field(default=0, description="Number of reference values")
    category_count: int = Field(default=0, description="Number of category paths")
    output_files: list[str] = Field(default=[], description="Paths to generated files")
    message: str = Field(default="", description="Summary of what happened")
    needs_human_input: bool = Field(default=False, description="True when avg mapping confidence < 0.7 or core column confidence < 0.8 — signals HITL required")
    core_column_detection: dict[str, str] = Field(default={}, description="Maps system-critical keys (sku/code/mrp/category) to source column names")
    mapping: list[ColumnMapping] = Field(default=[], description="Inferred column-to-PIM-attribute mappings")
    header_row: int = Field(default=0, description="Row index where column headers were found")
    data_start_row: int = Field(default=1, description="Row index where actual data starts")
    profiles: list[dict] = Field(default=[], description="Column profiles")
    category_hierarchy: list[str] = Field(default=[], description="Category paths")
    sheet_name: str = Field(default="", description="Sheet name used")


class AgentState(MessagesState):
    messages: Annotated[list, DeltaChannel(_messages_delta_reducer)]
    structured_response: Optional[IngestionOutput] = None
    remaining_steps: int = 25
    source_path: str
    sheet_name: Optional[str]
    sheet_count: int = 0
    sheets: list[str] = []
    column_count: int = 0
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
    core_column_detection: dict[str, str] = {}
    attribute_definitions: list[dict]
    reference_values: dict[str, list[str]]
    output_files: dict[str, str]
    need_user_input: bool
    human_approved: bool = False
    validation_errors: list[dict] = []
    validation_message: str = ""
    correction_cycle: int = 0
    error: Optional[str]
