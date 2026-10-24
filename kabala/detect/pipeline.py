"""Combines local rules and the model into one decision per email, with a confidence and a review flag."""

from __future__ import annotations

import logging

from kabala.detect import heuristics
from kabala.detect.llm import Classifier, LLMError
from kabala.detect.message import ParsedEmail
from kabala.detect.normalize import company_from_sender, normalize_company
from kabala.detect.result import Classification

log = logging.getLogger(__name__)

# Below this rule score an email is dropped without asking the model (saves cost on obvious noise).
SKIP_BELOW = 0.15


class Detector:
    def __init__(self, classifier: Classifier | None, threshold: float = 0.75) -> None:
        self.classifier = classifier
        self.threshold = threshold
        self.llm_errors: list[str] = []

    def detect(self, email: ParsedEmail) -> Classification:
        rules = heuristics.classify(email)
        if rules.confidence < SKIP_BELOW:
            return rules

        result = rules
        if self.classifier is not None:
            try:
                llm = self.classifier.classify(email)
                result = self._merge(rules, llm)
            except LLMError as e:
                log.warning("model check failed (%s); using local rules", e)
                self.llm_errors.append(str(e))

        result.company = normalize_company(result.company) or company_from_sender(email.sender_name, email.sender_email) or "Unknown"
        if not result.date:
            result.date = email.date
        if result.is_receipt:
            if result.amount is None:
                result.reason = "no_amount"
            elif result.confidence < self.threshold:
                result.reason = "low_confidence"
        return result

    @staticmethod
    def _merge(rules: Classification, llm: Classification) -> Classification:
        """The model decides; local rules fill gaps and temper confidence when they disagree."""
        merged = Classification(**llm.__dict__)
        if merged.amount is None and rules.amount is not None:
            merged.amount, merged.currency = rules.amount, rules.currency
        if not merged.currency:
            merged.currency = rules.currency
        if not merged.invoice_number:
            merged.invoice_number = rules.invoice_number
        if not merged.company:
            merged.company = rules.company
        agree = llm.is_receipt == rules.is_receipt
        amounts_agree = llm.amount is None or rules.amount is None or abs(llm.amount - rules.amount) < 0.01
        if agree and amounts_agree:
            merged.confidence = min(1.0, 0.75 * llm.confidence + 0.25 * max(rules.confidence, llm.confidence))
        else:
            merged.confidence = llm.confidence * 0.8  # disagreement: likely worth a human look
        merged.confidence = round(merged.confidence, 3)
        return merged

    def needs_review(self, c: Classification) -> bool:
        return c.is_receipt and bool(c.reason)
