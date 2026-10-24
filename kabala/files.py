"""Writing receipt PDFs into the folder tree, safely and without duplicates."""

from __future__ import annotations

import hashlib
import html
import os
import re
import tempfile
from pathlib import Path

from kabala.detect.message import ParsedEmail


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_unique(root: Path, segments: list[str], data: bytes) -> Path:
    """
    Writes `data` at root/segments, creating folders. If a different file already has that name,
    adds _2, _3… ; if the identical file is already there, returns its path without writing.
    The write is atomic (temp file + rename), so a crash never leaves half a PDF behind.
    """
    target = root.joinpath(*segments)
    target.parent.mkdir(parents=True, exist_ok=True)
    stem, suffix = target.stem, target.suffix
    digest = sha256(data)
    n = 1
    while target.exists():
        try:
            if sha256(target.read_bytes()) == digest:
                return target
        except OSError:
            pass
        n += 1
        target = target.with_name(f"{stem}_{n}{suffix}")
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".kabala-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


# ---- email body → PDF ---------------------------------------------------------------

_STRIP = [
    re.compile(r"<script\b.*?</script>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<img\b[^>]*>", re.IGNORECASE),  # remote images are never fetched (privacy, tracking pixels)
    re.compile(r"<link\b[^>]*>", re.IGNORECASE),
    re.compile(r"<iframe\b.*?</iframe>", re.IGNORECASE | re.DOTALL),
]


def _clean_html(doc: str) -> str:
    for rx in _STRIP:
        doc = rx.sub("", doc)
    return doc


def email_to_pdf(email: ParsedEmail) -> bytes:
    """
    Renders a receipt that lives in the email body as an A4 PDF, with a small header (subject, sender, date).
    Uses Qt's rich-text engine, so no browser or external tool is needed. Requires a Q(Gui)Application.
    """
    from PySide6.QtCore import QBuffer, QIODevice, QMarginsF, QSizeF
    from PySide6.QtGui import QFont, QPageLayout, QPageSize, QPdfWriter, QTextDocument

    rtl = bool(re.search(r"[֐-׿]", f"{email.subject}\n{email.text[:2000]}"))
    direction = "rtl" if rtl else "ltr"
    align = "right" if rtl else "left"
    header = (
        f'<table width="100%" cellpadding="2" style="border-bottom:1px solid #ccc; margin-bottom:12px;" dir="{direction}">'
        f'<tr><td align="{align}"><b style="font-size:13pt">{html.escape(email.subject)}</b></td></tr>'
        f'<tr><td align="{align}" style="color:#555">{html.escape(email.sender_name)} &lt;{html.escape(email.sender_email)}&gt;'
        f" · {html.escape(email.date)}</td></tr></table>"
    )
    if email.html:
        body = _clean_html(email.html)
    else:
        body = "<pre style='white-space:pre-wrap; font-family:inherit'>" + html.escape(email.text) + "</pre>"

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QPdfWriter(buffer)
    writer.setTitle(email.subject)
    writer.setCreator("Kabala")
    writer.setResolution(96)
    writer.setPageLayout(QPageLayout(QPageSize(QPageSize.PageSizeId.A4), QPageLayout.Orientation.Portrait, QMarginsF(12, 12, 12, 12), QPageLayout.Unit.Millimeter))

    doc = QTextDocument()
    doc.setDefaultFont(QFont("Arial", 10))
    doc.setHtml(f'<div dir="{direction}">{header}{body}</div>')
    rect = writer.pageLayout().paintRectPixels(writer.resolution())
    doc.setPageSize(QSizeF(rect.width(), rect.height()))
    doc.print_(writer)
    del writer  # finishes the PDF
    buffer.close()
    return bytes(buffer.data())
