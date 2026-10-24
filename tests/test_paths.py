from kabala.paths import receipt_path_segments, sanitize_segment


class TestSanitizeSegment:
    def test_keeps_hebrew_and_normal_names(self):
        assert sanitize_segment("חברת החשמל") == "חברת החשמל"
        assert sanitize_segment("Netflix") == "Netflix"

    def test_replaces_characters_illegal_on_windows(self):
        assert sanitize_segment('A/B:C*D?"E<F>G|H') == "A B C D E F G H"

    def test_strips_trailing_dots_and_spaces(self):
        assert sanitize_segment("Netflix Inc. ") == "Netflix Inc"

    def test_protects_reserved_device_names(self):
        assert sanitize_segment("CON") == "_CON"
        assert sanitize_segment("lpt1") == "_lpt1"

    def test_never_empty(self):
        assert sanitize_segment("  ///  ") == "Unknown"

    def test_caps_long_names(self):
        assert len(sanitize_segment("א" * 200)) <= 80


class TestReceiptPath:
    def test_company_first_matches_spec(self):
        assert receipt_path_segments("Netflix", "2026-10-14", 54.9, "ils", "company-first") == [
            "Netflix",
            "2026",
            "2026-10",
            "2026-10-14_Netflix_54.90ILS.pdf",
        ]

    def test_date_first(self):
        assert receipt_path_segments("Netflix", "2026-10-14", 54.9, "ILS", "date-first") == [
            "2026",
            "2026-10",
            "Netflix",
            "2026-10-14_Netflix_54.90ILS.pdf",
        ]

    def test_hebrew_company_uses_dashes_in_file_name(self):
        folder, _, _, file = receipt_path_segments("חברת החשמל", "2026-10-14", 412, "ILS", "company-first")
        assert folder == "חברת החשמל"
        assert file == "2026-10-14_חברת-החשמל_412.00ILS.pdf"
