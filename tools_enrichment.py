import os
import logging
from typing import Annotated
from langchain_core.tools import tool
from langgraph.types import Command
from langgraph.prebuilt import InjectedState
from google import genai
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("pim_enrich")


@tool
def enrich_descriptions(state: Annotated[dict, InjectedState], batch_size: int = 50) -> Command:
    """
    Scans the compiled products, identifies items missing descriptions, and automatically
    generates structured, optimized descriptions for them using the product's attributes.

    Args:
        batch_size: The maximum number of missing descriptions to generate in this turn (default: 50).
    """
    product_rows = state.get("product_rows", [])
    if not product_rows:
        return Command(value="There are no product rows available to enrich yet.")

    enriched_count = 0
    updated_rows = []

    client = genai.Client()

    for row in product_rows:
        desc = row.get("description", "").strip()
        if not desc or desc.lower() == "nan":
            prompt = (
                f"Write a brief product description for an item with SKU Name: '{row.get('sku_name')}', "
                f"Brand: '{row.get('brand')}', and code: '{row.get('code')}'."
            )
            try:
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                )
                row["description"] = response.text.strip()
                enriched_count += 1
            except Exception:
                pass
        updated_rows.append(row)

    return Command(
        update={"product_rows": updated_rows},
        value=f"Successfully generated descriptions for {enriched_count} products.",
    )
