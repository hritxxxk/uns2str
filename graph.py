import os
import json
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.prebuilt import create_react_agent
from state import AgentState, IngestionOutput, ColumnMapping
from helpers import read_file, get_headers_and_data, build_product_rows, extract_image_columns, fingerprint_headers, load_cached_mapping, save_cached_mapping
from tools.profiling import profile_file
from tools.mapping import build_attribute_definitions
from tools.references import extract_reference_values


load_dotenv()
api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
os.environ["GOOGLE_API_KEY"] = api_key or ""

model = ChatGoogleGenerativeAI(model="gemini-2.5-flash-lite")

tools = [profile_file]

SYSTEM_PROMPT = """You are a PIM ingestion agent. You have one tool: profile_file.
1. Call profile_file to understand the source file structure
2. Map columns to PIM attributes yourself (return in IngestionOutput.mapping)

Rules:
- PIM defaults: sku_name, code, description, mrp, brand (do not recreate)
- target_attribute: snake_case
- attribute_type: Dropdown for brand/colour/size/gender/season/category, RichText for descriptions,
  Textbox for codes/names/numbers, MultiSelect/MultiSelectDropdown for multi-value,
  MultiTextBox for bullet points, Date for dates
- attribute_data_type: varchar, varchar[], int, float, boolean, date
- constraint=true only for Dropdown, MultiSelect, MultiSelectDropdown, MultiTextBox
- mandatory=true only for sku, code, product_name, mrp

Map EVERY source column. Give unmapped columns a generic Textbox with confidence 0.5.
Return IngestionOutput with mapping, category_hierarchy, profiles, header_row, data_start_row."""

graph = create_react_agent(model, tools, prompt=SYSTEM_PROMPT, response_format=IngestionOutput, state_schema=AgentState)
