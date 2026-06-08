"""State schemas for the interactive 4-phase VinGPT graph.

IngestionPhase — tracks which milestone the conversation is in.
PhaseOutput   — captures AI explanation + user feedback per phase.
InteractiveIngestionState — full graph state with phase tracking.
"""

from typing import Optional, TypedDict
from enum import Enum


class IngestionPhase(str, Enum):
    CATEGORIES = "categories"
    ATTRIBUTES = "attributes"
    REFERENCES = "references"
    PRODUCTS = "products"


class PhaseOutput(TypedDict):
    """Container for one phase's AI reasoning + user resolution.

    Fields
    ------
    explanation : str
        Glass-clear narrative shown in the chat bubble.
    reasoning : str
        Deeper technical rationale (shown if user taps "why?").
    suggestions : list[dict]
        Structured items for user review. Each item has:
          - type: "group" | "item"
          - label: str
          - items: list[dict] (for groups; nested suggestions)
          - column / mapped_to / confidence / reasoning / options (for items)
    approved : bool
        True once the user has confirmed this phase.
    user_feedback : str
        Freeform user edits / corrections from the chat.
    """
    explanation: str
    reasoning: str
    suggestions: list[dict]
    approved: bool
    user_feedback: str


class InteractiveIngestionState(TypedDict):
    """Full graph state for the interactive 4-phase onboarding chat.

    Phase tracking fields let the graph know where to resume after
    each user-interrupt cycle. Per-phase outputs store the LLM's
    reasoning and the user's resolution for later rendering.
    """
    # ── Conversation ────────────────────────────────────────
    messages: list
    file_path: str
    sheet_name: str | None
    profile_data: dict | None

    # ── Phase tracking ──────────────────────────────────────
    current_phase: str       # "categories"|"attributes"|"references"|"products"|"complete"
    phases_completed: list   # e.g. ["categories", "attributes"]

    # ── Per-phase outputs ───────────────────────────────────
    categories: PhaseOutput
    attributes: PhaseOutput
    references: PhaseOutput
    products: PhaseOutput

    # ── Multi-sheet merge ───────────────────────────────────
    all_sheets: list
    sheet_merge: dict

    # ── Preserved for downstream rendering ──────────────────
    core_mappings: dict[str, str]
    custom_mappings: dict[str, str]
    mapping_confidence: dict[str, int]
    product_rows: list[dict]
    generated_files: list[str]
    jwt_token: str
