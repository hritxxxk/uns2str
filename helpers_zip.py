import os
import zipfile
import tempfile
import json
import logging

from helpers import read_file, take_rows

logger = logging.getLogger("pim_zip")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    logger.addHandler(handler)


def extract_zip(zip_path: str) -> tuple[str, list[str]]:
    """Extract all CSV and XLSX files from a ZIP archive to a temp directory.

    Returns (temp_dir_path, list_of_extracted_filenames).
    Caller must call cleanup_temp(temp_dir_path) when done.
    """
    temp_dir = tempfile.mkdtemp(prefix="pim_zip_")
    extracted = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            ext = os.path.splitext(name)[1].lower()
            if ext not in (".csv", ".xlsx", ".xls"):
                continue
            safe_name = os.path.basename(name)
            if not safe_name:
                continue
            target = os.path.join(temp_dir, safe_name)
            with zf.open(name) as source, open(target, "wb") as dest:
                dest.write(source.read())
            extracted.append(safe_name)
    logger.info(f"extracted {len(extracted)} files from {zip_path}")
    return temp_dir, sorted(extracted)


def cleanup_temp(temp_dir: str) -> None:
    """Remove a temp directory and all its contents."""
    import shutil
    shutil.rmtree(temp_dir, ignore_errors=True)
    logger.info(f"cleaned up {temp_dir}")


def profile_files(dir_path: str, file_list: list[str]) -> dict:
    """Read first 50 rows of each file to extract headers and sample values.

    Returns dict: {filename: {headers, samples, row_count, ext}}
    """
    profiles = {}
    for fname in file_list:
        fpath = os.path.join(dir_path, fname)
        ext = os.path.splitext(fname)[1].lower()
        try:
            gen = read_file(fpath)
            first_rows = take_rows(gen, 50)
            if not first_rows:
                continue
            headers = [str(c).strip() if c else "" for c in first_rows[0]]
            headers = [h for h in headers if h]
            samples = []
            for row in first_rows[1:6]:
                s = {}
                for i, h in enumerate(headers):
                    if i < len(row) and row[i] is not None and str(row[i]).strip():
                        s[h] = str(row[i]).strip()[:60]
                if s:
                    samples.append(s)
            profiles[fname] = {
                "headers": headers,
                "samples": samples,
                "row_count": len(first_rows) - 1,
                "ext": ext,
            }
        except Exception as exc:
            logger.warning(f"profile failed for {fname}: {exc}")
    return profiles
