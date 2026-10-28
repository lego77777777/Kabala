"""
The receipt PDF for Stripe payments, with fpdf2 (github.com/py-pdf/fpdf2, used as a library: it is LGPL) and
HarfBuzz text shaping, so Hebrew is laid out right to left with numbers and Latin text in their natural order.
Same content as the PayPal server's receipts (billing/src/pdf/receipt-document.tsx), in the Heebo font.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fpdf import FPDF

FONTS = Path(__file__).with_name("admin_ui") / "fonts"

TEXTS = {
    "he": {
        "dir": "rtl", "receipt": "קבלה", "tax_receipt": "חשבונית מס/קבלה", "number": "מספר", "original": "מקור",
        "issued": "תאריך", "customer": "לקוח", "description": "תיאור", "period": "תקופה", "method": "אמצעי תשלום",
        "card": "כרטיס אשראי (Stripe)", "invoice": "חשבונית Stripe", "net": "סכום לפני מע\"מ", "vat": "מע\"מ {rate}%",
        "total": "סה\"כ שולם", "plan": {"month": "Kabala Pro – מנוי חודשי", "year": "Kabala Pro – מנוי שנתי"},
        "exempt": "עוסק פטור", "licensed": "עוסק מורשה", "footer": "מסמך ממוחשב. הופק אוטומטית על ידי Kabala.",
    },
    "en": {
        "dir": "ltr", "receipt": "Receipt", "tax_receipt": "Tax invoice / receipt", "number": "No.", "original": "Original",
        "issued": "Date", "customer": "Customer", "description": "Description", "period": "Period", "method": "Payment method",
        "card": "Credit card (Stripe)", "invoice": "Stripe invoice", "net": "Amount before VAT", "vat": "VAT {rate}%",
        "total": "Total paid", "plan": {"month": "Kabala Pro – monthly", "year": "Kabala Pro – yearly"},
        "exempt": "Exempt dealer", "licensed": "Licensed dealer", "footer": "Computer-generated document, issued automatically by Kabala.",
    },
}


@dataclass
class Seller:
    name: str
    tax_id: str
    address: str
    email: str
    business_type: str  # exempt | licensed
    vat_rate_percent: float


@dataclass
class ReceiptData:
    number: int
    issued: str  # dd/mm/yyyy
    customer_name: str
    customer_email: str
    plan_interval: str | None  # month | year
    period: str  # e.g. "מ-28/10/2026 עד 28/11/2026" (period_text), or ""
    amount: str  # formatted total, e.g. ₪29.00
    net: str | None  # licensed dealers only
    vat: str | None
    stripe_invoice_id: str
    locale: str = "he"


def period_text(start: str, end: str, locale: str) -> str:
    """Words, not a dash: in a right-to-left line, "date – date" is shown with the two dates swapped."""
    return f"מ-{start} עד {end}" if locale == "he" else f"{start} – {end}"


def title(seller: Seller, locale: str) -> str:
    t = TEXTS[locale]
    return t["tax_receipt"] if seller.business_type == "licensed" else t["receipt"]


def build_pdf(r: ReceiptData, seller: Seller) -> bytes:
    t = TEXTS.get(r.locale, TEXTS["he"])
    rtl = t["dir"] == "rtl"
    start, end = ("R", "L") if rtl else ("L", "R")
    pdf = FPDF(format="A4")
    pdf.set_margins(20, 20, 20)
    pdf.set_auto_page_break(True, 20)
    pdf.add_page()
    pdf.add_font("Heebo", "", str(FONTS / "Heebo-Regular.ttf"))
    pdf.add_font("Heebo", "B", str(FONTS / "Heebo-Bold.ttf"))
    pdf.set_text_shaping(use_shaping_engine=True, direction=t["dir"])
    pdf.set_title(f"{title(seller, r.locale)} {r.number}")
    pdf.set_author(seller.name)
    pdf.set_creator("Kabala")
    width = pdf.epw

    # seller
    pdf.set_font("Heebo", "B", 20)
    pdf.set_text_color(79, 70, 229)
    pdf.cell(width, 11, seller.name, align=start, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Heebo", "", 10)
    pdf.set_text_color(100, 100, 110)
    kind = t["licensed"] if seller.business_type == "licensed" else t["exempt"]
    for line in (f"{kind} {seller.tax_id}".strip(), seller.address, seller.email):
        if line:
            pdf.cell(width, 6, line, align=start, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)

    # title
    pdf.set_text_color(24, 24, 27)
    pdf.set_font("Heebo", "B", 18)
    pdf.cell(width, 10, f"{title(seller, r.locale)} {t['number']} {r.number}", align=start, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Heebo", "", 10)
    pdf.set_text_color(100, 100, 110)
    pdf.cell(width, 6, t["original"], align=start, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    def row(label: str, value: str, bold: bool = False) -> None:
        pdf.set_draw_color(228, 228, 231)
        pdf.set_font("Heebo", "B" if bold else "", 13 if bold else 11)
        pdf.set_text_color(24, 24, 27) if bold else pdf.set_text_color(100, 100, 110)
        y = pdf.get_y()
        pdf.set_x(pdf.l_margin + (width * 0.6 if rtl else 0))  # the label sits at the start edge (right in Hebrew)
        pdf.cell(width * 0.4, 10, label, align=start)
        pdf.set_x(pdf.l_margin)
        pdf.set_text_color(24, 24, 27)
        pdf.cell(width, 10, value, align=end, new_x="LMARGIN", new_y="NEXT")
        pdf.line(pdf.l_margin, y + 10, pdf.l_margin + width, y + 10)

    row(t["issued"], r.issued)
    row(t["customer"], " · ".join(x for x in (r.customer_name, r.customer_email) if x))
    row(t["description"], t["plan"].get(r.plan_interval or "month", t["plan"]["month"]))
    if r.period:
        row(t["period"], r.period)
    row(t["method"], t["card"])
    row(t["invoice"], r.stripe_invoice_id)
    if r.net and r.vat:
        row(t["net"], r.net)
        row(t["vat"].format(rate=f"{seller.vat_rate_percent:g}"), r.vat)
    row(t["total"], r.amount, bold=True)

    pdf.ln(14)
    pdf.set_font("Heebo", "", 9)
    pdf.set_text_color(140, 140, 150)
    pdf.cell(width, 6, t["footer"], align="C", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
