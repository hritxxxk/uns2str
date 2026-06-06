import os
import logging

logger = logging.getLogger("pim_learning")

DATASET_NAME = "pim-mapping-corrections"


def _get_client():
    api_key = os.getenv("LANGSMITH_API_KEY")
    if not api_key:
        return None
    try:
        from langsmith import Client
        return Client(api_key=api_key)
    except Exception as e:
        logger.warning(f"Failed to init LangSmith client: {e}")
        return None


def _ensure_dataset(client):
    try:
        ds = client.read_dataset(dataset_name=DATASET_NAME)
        return ds
    except Exception:
        return client.create_dataset(
            dataset_name=DATASET_NAME,
            description="Human-corrected PIM column mappings for few-shot learning",
        )


def log_corrections(mapping_updates, profiles, fingerprint=None):
    client = _get_client()
    if not client:
        logger.info("No LANGSMITH_API_KEY set — skipping ContextHub storage")
        return

    dataset = _ensure_dataset(client)
    profile_lookup = {p.get("name", ""): p for p in (profiles or [])}

    count = 0
    for m in mapping_updates:
        src = m.source_column
        if not src:
            continue
        prof = profile_lookup.get(src, {})
        sample_values = prof.get("sample_values", prof.get("samples", []))
        if not sample_values and prof.get("unique_values"):
            sample_values = prof["unique_values"][:10]

        try:
            client.create_example(
                dataset_name=DATASET_NAME,
                inputs={
                    "column_name": src,
                    "sample_values": sample_values,
                    "fingerprint": fingerprint or "",
                },
                outputs={
                    "target_attribute": m.target_attribute,
                    "attribute_type": m.attribute_type,
                    "attribute_data_type": m.attribute_data_type,
                    "constraint": m.constraint,
                    "length": m.length,
                    "mandatory": m.mandatory,
                    "attribute_group": m.attribute_group,
                },
            )
            count += 1
        except Exception as e:
            logger.warning(f"Failed to log correction for '{src}': {e}")

    logger.info(f"Logged {count}/{len(mapping_updates)} corrections to ContextHub")


def fetch_similar_examples(column_name, sample_values, k=5):
    client = _get_client()
    if not client:
        return []

    try:
        dataset = client.read_dataset(dataset_name=DATASET_NAME)
    except Exception:
        return []

    try:
        results = client.similar_examples(
            inputs={
                "column_name": column_name,
                "sample_values": sample_values,
            },
            dataset_id=dataset.id,
            limit=k,
        )
    except Exception as e:
        logger.warning(f"ContextHub similarity search failed: {e}")
        return []

    formatted = []
    for r in results:
        inp = r.get("inputs", {})
        out = r.get("outputs", {})
        formatted.append({
            "column_name": inp.get("column_name", ""),
            "sample_values": inp.get("sample_values", []),
            "target_attribute": out.get("target_attribute", ""),
            "attribute_type": out.get("attribute_type", ""),
            "attribute_data_type": out.get("attribute_data_type", ""),
            "constraint": out.get("constraint", False),
            "mandatory": out.get("mandatory", False),
            "attribute_group": out.get("attribute_group", ""),
        })

    return formatted
