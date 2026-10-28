"""
The receipt email's subject and opening line, editable in the admin panel, as mustache templates rendered with
chevron (vendor/chevron): "הקבלה שלך מ-Kabala – {{month}}". The same rules as the PayPal server
(billing/src/receipts/templates.ts), so one template works on both servers.

Only plain {{variable}} tags from VARIABLES are allowed. Sections, partials, unescaped {{{ }}} / {{& }} and
changed delimiters are refused with a message naming the tag. Values go in as plain text (the email's HTML is
escaped by the caller), and line breaks are removed from the subject, so it can't add mail headers.
"""

from __future__ import annotations

import re

from kabala_server.vendor import chevron
from kabala_server.vendor.chevron.tokenizer import tokenize

VARIABLES = {
    "name": "שם הלקוח",
    "number": "מספר הקבלה",
    "amount": "הסכום",
    "month": "החודש",
    "plan": "המסלול",
    "business": "שם העסק",
}
LIMITS = {"subject": 150, "intro": 1000}


class TemplateError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def check_template(field: str, template: str) -> None:
    if len(template) > LIMITS[field]:
        raise TemplateError(field, f"ארוך מדי (עד {LIMITS[field]} תווים)")
    try:
        tokens = list(tokenize(template))
    except Exception as e:  # noqa: BLE001 - chevron raises ChevronError and others for broken tags
        raise TemplateError(field, f"תבנית לא תקינה: {e}") from e
    for kind, value in tokens:
        if kind == "literal":
            continue
        if kind != "variable":
            raise TemplateError(field, f'מותר רק {{{{משתנה}}}} פשוט (לא "{kind} {value}")')
        if value not in VARIABLES:
            allowed = " ".join(f"{{{{{k}}}}}" for k in VARIABLES)
            raise TemplateError(field, f"אין משתנה בשם {{{{{value}}}}}. אפשר: {allowed}")


def render_template(field: str, template: str, values: dict[str, str]) -> str:
    check_template(field, template)
    out = chevron.render(template, values, escape_html=False)
    return re.sub(r"[\r\n]+", " ", out).strip() if field == "subject" else out.strip()
