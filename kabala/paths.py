"""Folder structure and safe file names for saved receipts."""

from __future__ import annotations

import re
import unicodedata

from kabala.models import FolderLayout

# Characters not allowed in file names on Windows (and "/" everywhere), plus control chars.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.IGNORECASE)
MAX_SEGMENT = 80


def sanitize_segment(text: str) -> str:
    """Makes a string safe as a single folder or file name on Windows, macOS and Linux. Keeps Hebrew."""
    out = unicodedata.normalize("NFC", text)
    out = _ILLEGAL.sub(" ", out)
    out = re.sub(r"\s+", " ", out).strip()
    # Windows silently strips trailing dots and spaces, which breaks lookups later.
    out = re.sub(r"[. ]+$", "", out)
    if not out:
        out = "Unknown"
    if _RESERVED.match(out):
        out = f"_{out}"
    return out[:MAX_SEGMENT].strip()


def format_amount_for_file(amount: float, currency: str) -> str:
    return f"{amount:.2f}{currency.upper()}"


def receipt_path_segments(company: str, date: str, amount: float, currency: str, layout: FolderLayout) -> list[str]:
    """
    company-first: <Company>/<YYYY>/<YYYY-MM>/<YYYY-MM-DD>_<Company>_<amount><CUR>.pdf
    date-first:    <YYYY>/<YYYY-MM>/<Company>/<YYYY-MM-DD>_<Company>_<amount><CUR>.pdf
    """
    safe_company = sanitize_segment(company)
    year, month = date[:4], date[:7]
    file_company = safe_company.replace(" ", "-")
    file_name = sanitize_segment(f"{date}_{file_company}_{format_amount_for_file(amount, currency)}") + ".pdf"
    if layout == "company-first":
        return [safe_company, year, month, file_name]
    return [year, month, safe_company, file_name]
