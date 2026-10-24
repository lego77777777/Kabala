import base64
import json
from types import SimpleNamespace

import pytest

from kabala.detect import heuristics
from kabala.detect.llm import AnthropicClassifier, LLMError, build_prompt, redact
from kabala.detect.message import parse_gmail_message
from kabala.detect.normalize import company_from_sender, normalize_company
from kabala.detect.pipeline import Detector
from kabala.detect.query import build_query
from kabala.detect.result import Classification
from tests.samples import SAMPLES

# ---- company names -------------------------------------------------------------


@pytest.mark.parametrize("raw", ["Netflix.com", "NETFLIX INC", "Netflix", "netflix", "www.netflix.com", "Netflix, Inc."])
def test_netflix_variants_become_one_folder(raw):
    assert normalize_company(raw) == "Netflix"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Amazon.com Services LLC", "Amazon"),
        ("AMZN Mktp US", "Amazon"),
        ('בזק בע"מ', "בזק"),
        ("בזק בע״מ", "בזק"),
        ("חברת החשמל לישראל", "חברת החשמל"),
        ("super-pharm.co.il", "סופר-פארם"),
        ("Google Payments", "Google"),
        ("ACME WIDGETS LTD", "Acme Widgets"),
        ("eBay", "eBay"),
        ("מאפיית אבולעפיה", "מאפיית אבולעפיה"),
    ],
)
def test_normalize_company(raw, expected):
    assert normalize_company(raw) == expected


def test_generic_sender_falls_back_to_domain():
    assert company_from_sender("no-reply", "receipts@wolt.com") == "Wolt"
    assert company_from_sender("", "billing@account.netflix.com") == "Netflix"
    assert company_from_sender("Billing", "invoice@my-small-shop.co.il") == "My-Small-Shop"


# ---- search query ----------------------------------------------------------------


def test_query_covers_hebrew_english_pdf_and_senders():
    from datetime import date

    q = build_query(after=date(2026, 1, 1))
    for part in ["subject:קבלה", 'subject:"חשבונית מס"', 'subject:"אישור תשלום"', "subject:receipt",
                 'subject:"order confirmation"', "filename:pdf", "from:netflix.com", "after:2026/01/01"]:
        assert part in q
    assert q.startswith("{") and "-in:chats" in q


# ---- parsing a Gmail API message ---------------------------------------------------


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def test_parse_gmail_message_html_hebrew_and_attachment():
    msg = {
        "id": "18c", "threadId": "18c", "internalDate": "1792000000000", "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": '"Netflix" <info@account.netflix.com>'},
                {"name": "Subject", "value": "הקבלה שלך"},
                {"name": "Message-ID", "value": "<abc@netflix.com>"},
            ],
            "parts": [
                {"mimeType": "multipart/alternative", "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64("plain version")}},
                    {"mimeType": "text/html", "headers": [{"name": "Content-Type", "value": "text/html; charset=UTF-8"}],
                     "body": {"data": _b64("<html><style>p{}</style><table><tr><td>סה\"כ</td><td>₪54.90</td></tr></table>"
                                           "<p>תודה&nbsp;רבה</p></html>")}},
                ]},
                {"mimeType": "application/pdf", "filename": "receipt.pdf", "body": {"attachmentId": "ATT1", "size": 1234}},
            ],
        },
    }
    e = parse_gmail_message(msg)
    assert e.sender_name == "Netflix" and e.sender_email == "info@account.netflix.com"
    assert e.message_id == "<abc@netflix.com>"
    assert 'סה"כ ₪54.90' in e.text and "תודה רבה" in e.text and "p{}" not in e.text
    assert [a.filename for a in e.pdfs] == ["receipt.pdf"] and e.pdfs[0].attachment_id == "ATT1"
    assert len(e.date) == 10


# ---- local rules -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, amount, currency",
    [
        ('סה"כ: ₪54.90', 54.90, "ILS"),
        ("סכום לתשלום: 1,234.50 ש\"ח", 1234.50, "ILS"),
        ("Subtotal: $45.98\nOrder Total: $49.20", 49.20, "USD"),
        ("Amount paid US$22.99", 22.99, "USD"),
        ("Total €19", 19.0, "EUR"),
        ("item 10.00 ₪\nitem 25.00 ₪", 25.0, "ILS"),
    ],
)
def test_extract_amount(text, amount, currency):
    assert heuristics.extract_amount(text) == (amount, currency)


def test_extract_invoice_number():
    assert heuristics.extract_invoice_number("חשבונית מס מספר 4455667") == "4455667"
    assert heuristics.extract_invoice_number("Invoice No. INV-2026-0042") == "INV-2026-0042"
    assert heuristics.extract_invoice_number("Order #112-1234567-1234567") == "112-1234567-1234567"


def test_rules_accuracy_on_labeled_samples():
    """Success criterion: >90% correct receipt / not-receipt decisions (local rules alone, no model)."""
    correct = sum(heuristics.classify(e).is_receipt == label for e, label, *_ in SAMPLES)
    accuracy = correct / len(SAMPLES)
    wrong = [e.subject for e, label, *_ in SAMPLES if heuristics.classify(e).is_receipt != label]
    assert accuracy > 0.9, f"accuracy {accuracy:.0%}, wrong: {wrong}"


def test_rules_extract_amounts_on_labeled_receipts():
    receipts = [(e, amt, cur) for e, label, _, amt, cur in SAMPLES if label]
    ok = sum(heuristics.classify(e).amount == amt and heuristics.classify(e).currency == cur for e, amt, cur in receipts)
    assert ok / len(receipts) > 0.9


def test_company_names_on_labeled_receipts():
    for e, label, company, *_ in SAMPLES:
        if label and company:
            assert Detector(None).detect(e).company == company, e.subject


# ---- model verification (Claude) ---------------------------------------------------------


class FakeMessages:
    def __init__(self, payload, stop_reason="end_turn"):
        self.payload = payload
        self.stop_reason = stop_reason
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        block = SimpleNamespace(type="text", text=json.dumps(self.payload))
        return SimpleNamespace(content=[block], stop_reason=self.stop_reason)


def _claude(payload, stop_reason="end_turn"):
    fake = FakeMessages(payload, stop_reason)
    return AnthropicClassifier(SimpleNamespace(messages=fake)), fake


def test_claude_request_shape_and_parsing():
    email = SAMPLES[9][0]  # PayPal → Valve
    clf, fake = _claude({"is_receipt": True, "company": "Steam", "date": "2026-10-14", "amount": 19.99,
                          "currency": "usd", "invoice_number": "8AB12345CD678901E", "confidence": 0.97})
    result = clf.classify(email)
    assert result.company == "Steam" and result.amount == 19.99 and result.currency == "USD"
    call = fake.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "Valve" in call["messages"][0]["content"]


def test_claude_refusal_raises_and_pipeline_falls_back_to_rules():
    clf, _ = _claude({}, stop_reason="refusal")
    with pytest.raises(LLMError):
        clf.classify(SAMPLES[0][0])
    detector = Detector(clf)
    result = detector.detect(SAMPLES[0][0])
    assert result.source == "rules" and result.is_receipt and detector.llm_errors == ["refusal"]


def test_model_overrides_rules_and_low_confidence_goes_to_review():
    clf, _ = _claude({"is_receipt": True, "company": "NETFLIX INC", "date": "2026-10-14", "amount": 54.9,
                      "currency": "ILS", "invoice_number": "", "confidence": 0.6})
    result = Detector(clf, threshold=0.75).detect(SAMPLES[0][0])
    assert result.company == "Netflix"
    assert result.reason == "low_confidence"


def test_obvious_noise_never_reaches_the_model():
    clf, fake = _claude({})
    Detector(clf).detect(SAMPLES[-1][0])  # store newsletter
    assert fake.calls == []


def test_privacy_redaction_and_no_full_sender_address():
    text = "Card 4580 1234 5678 9012, contact me at dana@example.com, ID 123456782, total ₪10"
    assert redact(text) == "Card [CARD], contact me at [EMAIL], ID [ID], total ₪10"
    prompt = build_prompt(SAMPLES[0][0])
    assert "info@account.netflix.com" not in prompt and "account.netflix.com" in prompt


def test_merge_disagreement_lowers_confidence():
    rules = Classification(True, "X", "2026-01-01", 10.0, "ILS", "", 0.9)
    llm = Classification(True, "X", "2026-01-01", 99.0, "ILS", "", 0.9, source="anthropic")
    assert Detector._merge(rules, llm).confidence < 0.75
