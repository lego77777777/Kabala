from datetime import date

from kabala.demo import generate_mailbox, run_demo_scan
from kabala.models import ScanOptions


def test_mailbox_is_deterministic_and_ends_today():
    today = date(2026, 10, 20)
    a = generate_mailbox(today)
    b = generate_mailbox(today)
    assert [r.id for r in a] == [r.id for r in b]
    assert max(r.date for r in a) <= "2026-10-20"
    assert min(r.date for r in a) >= "2026-05-01"


def test_scan_splits_by_threshold_and_skips_known_messages():
    opts = ScanOptions(folder="/tmp/x", layout="company-first", threshold=0.75)
    events = []
    found, review = run_demo_scan(opts, set(), events.append, lambda: False, delay=0)
    assert found and all(r.confidence >= 0.75 for r in found)
    assert all(r.path_segments[-1].endswith(".pdf") for r in found)
    assert events[-1].phase == "done"
    # every receipt in the mailbox is either found or sent to review
    mailbox = generate_mailbox()
    assert len(found) + sum(1 for r in review if r.id.startswith("r")) == len(mailbox)

    again, _ = run_demo_scan(opts, {r.message_id for r in found}, lambda _p: None, lambda: False, delay=0)
    assert again == []
    assert review  # noise + low-confidence items


def test_scan_can_be_canceled():
    opts = ScanOptions(folder="/tmp/x", layout="date-first", threshold=0.75)
    found, _ = run_demo_scan(opts, set(), lambda _p: None, lambda: True, delay=0)
    assert found == []
