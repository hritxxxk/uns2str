import os
import polars as pl


def _get_lazy_frame(path, sheet_name=None):
    if path.endswith(".csv"):
        return pl.scan_csv(path)
    df = pl.read_excel(path, sheet_name=sheet_name, engine="calamine")
    return df.lazy()


def profile_large_file(path, sheet_name=None):
    lazy = _get_lazy_frame(path, sheet_name)

    schema = lazy.collect_schema()
    headers = schema.names()

    stats = lazy.select([
        pl.all().null_count().name.suffix("_nulls"),
        pl.all().n_unique().name.suffix("_uniques"),
    ]).collect(streaming=True)

    profiles = []
    for col in headers:
        null_count = stats[f"{col}_nulls"][0]
        unique_count = stats[f"{col}_uniques"][0]
        sample_values = lazy.select(pl.col(col)).limit(5).collect()[col].to_list()
        sample_values_clean = [str(v) if v is not None else "" for v in sample_values]

        profiles.append({
            "column_name": col,
            "null_count": int(null_count),
            "unique_count": int(unique_count),
            "sample_values": sample_values_clean,
            "data_type": str(schema[col]),
        })

    row_count = lazy.select(pl.len()).collect()[0, 0]
    sample_rows_df = lazy.limit(3).collect()
    sample_rows = sample_rows_df.to_dicts()

    return {
        "headers": headers,
        "profiles": profiles,
        "row_count": int(row_count),
        "sample_rows": sample_rows,
    }


def extract_categories_polars(path, columns, sheet_name=None):
    lazy = _get_lazy_frame(path, sheet_name)
    schema = lazy.collect_schema()
    headers = schema.names()

    cols_to_use = [col.strip() for col in columns if col.strip() in headers]
    if not cols_to_use:
        return []

    df_categories = lazy.select(cols_to_use).unique().collect(streaming=True)
    paths = set()

    for row in df_categories.iter_rows():
        nodes = [
            str(val).strip()
            for val in row
            if val is not None
            and str(val).strip().lower() != "nan"
            and str(val).strip() != ""
        ]
        if nodes:
            paths.add(" > ".join(nodes))

    return sorted(list(paths))


def stream_product_export_polars(source_path, core_mappings, output_path, sheet_name=None):
    lazy = _get_lazy_frame(source_path, sheet_name)
    schema = lazy.collect_schema()
    headers = schema.names()

    projection = {}
    for target_pim, source_col in core_mappings.items():
        if source_col in headers:
            projection[target_pim] = pl.col(source_col)

    lazy_mapped = lazy.select(**projection)

    if output_path.endswith(".csv"):
        lazy_mapped.sink_csv(output_path)
    else:
        df_mapped = lazy_mapped.collect(streaming=True)
        df_mapped.write_excel(output_path)
