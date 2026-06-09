import logging
from typing import Annotated
from langchain_core.tools import tool
from langgraph.types import Command
from langgraph.prebuilt import InjectedState

logger = logging.getLogger("pim_merge")


def _jaro_winkler_similarity(s1: str, s2: str) -> float:
    """Compute Jaro-Winkler similarity between two strings."""
    if not s1 or not s2:
        return 0.0
    s1, s2 = s1.lower().strip(), s2.lower().strip()
    if s1 == s2:
        return 1.0

    len_s1, len_s2 = len(s1), len(s2)
    match_dist = max(len_s1, len_s2) // 2 - 1
    if match_dist < 0:
        match_dist = 0

    s1_matches = [False] * len_s1
    s2_matches = [False] * len_s2
    matches = 0
    transpositions = 0

    for i in range(len_s1):
        start = max(0, i - match_dist)
        end = min(i + match_dist + 1, len_s2)
        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    k = 0
    for i in range(len_s1):
        if s1_matches[i]:
            while not s2_matches[k]:
                k += 1
            if s1[i] != s2[k]:
                transpositions += 1
            k += 1

    jaro = (matches / len_s1 + matches / len_s2 + (matches - transpositions / 2) / matches) / 3
    prefix = 0
    for i in range(min(4, len_s1, len_s2)):
        if s1[i] == s2[i]:
            prefix += 1
        else:
            break
    return jaro + 0.1 * prefix * (1 - jaro)


def find_near_duplicates_jw(rows: list[dict], threshold: float = 0.85) -> list[dict]:
    """Cluster near-duplicate products by SKU name similarity."""
    groups = []
    seen = set()
    for i, a in enumerate(rows):
        if i in seen:
            continue
        name_a = str(a.get("sku_name", a.get("code", ""))).lower().strip()
        if not name_a:
            continue
        cluster = [i]
        for j, b in enumerate(rows):
            if j <= i or j in seen:
                continue
            name_b = str(b.get("sku_name", b.get("code", ""))).lower().strip()
            if not name_b:
                continue
            sim = _jaro_winkler_similarity(name_a, name_b)
            if sim >= threshold:
                cluster.append(j)
                seen.add(j)
        if len(cluster) > 1:
            groups.append({
                "base_idx": cluster[0],
                "duplicate_indices": cluster[1:],
                "similarities": [
                    round(_jaro_winkler_similarity(
                        str(rows[cluster[0]].get("sku_name", "")),
                        str(rows[c].get("sku_name", "")),
                    ), 3)
                    for c in cluster[1:]
                ],
            })
            seen.update(cluster)
    return groups


@tool
def merge_duplicates(state: Annotated[dict, InjectedState], similarity_threshold: float = 0.85) -> Command:
    """
    Scans the compiled product rows and calculates title similarity to find near-duplicate items.
    Returns the duplicate candidate groups for user approval.

    Args:
        similarity_threshold: Float boundary representing string match similarity (default: 0.85).
    """
    product_rows = state.get("product_rows", [])
    if not product_rows:
        return Command(value="No product rows are available to scan for duplicates.")

    duplicate_groups = find_near_duplicates_jw(product_rows, similarity_threshold)

    if not duplicate_groups:
        return Command(
            value=f"I scanned your products and found zero duplicate groups at a {similarity_threshold} threshold."
        )

    return Command(
        update={"merge_candidates": duplicate_groups},
        value=f"I identified {len(duplicate_groups)} groups of potential duplicates at a {similarity_threshold} similarity threshold.",
    )
