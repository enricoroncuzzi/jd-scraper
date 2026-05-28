from src.models import JobOffer
from src.dedup import filter_new, mark_seen


def test_filter_new_returns_all_when_log_absent(tmp_path):
    offers = [JobOffer(id=0, title="t", company="c", link="https://li.com/1")]
    result = filter_new(offers, str(tmp_path / "seen.txt"))
    assert len(result) == 1


def test_filter_new_removes_seen_offer(tmp_path):
    log = str(tmp_path / "seen.txt")
    offer = JobOffer(id=0, title="t", company="c", link="https://li.com/1")
    mark_seen([offer], log)
    result = filter_new([offer], log)
    assert result == []


def test_filter_new_keeps_unseen_and_removes_seen(tmp_path):
    log = str(tmp_path / "seen.txt")
    seen = JobOffer(id=0, title="t", company="c", link="https://li.com/1")
    new = JobOffer(id=1, title="t", company="c", link="https://li.com/2")
    mark_seen([seen], log)
    result = filter_new([seen, new], log)
    assert len(result) == 1
    assert result[0].link == "https://li.com/2"


def test_mark_seen_creates_log_file(tmp_path):
    log = tmp_path / "seen.txt"
    offer = JobOffer(id=0, title="t", company="c", link="https://li.com/1")
    mark_seen([offer], str(log))
    assert log.exists()
    lines = [l for l in log.read_text().strip().split("\n") if l]
    assert len(lines) == 1
    assert len(lines[0]) == 32  # MD5 hex digest length


def test_mark_seen_appends_across_calls(tmp_path):
    log = str(tmp_path / "seen.txt")
    o1 = JobOffer(id=0, title="t", company="c", link="https://li.com/1")
    o2 = JobOffer(id=1, title="t", company="c", link="https://li.com/2")
    mark_seen([o1], log)
    mark_seen([o2], log)
    lines = [l for l in open(log).read().strip().split("\n") if l]
    assert len(lines) == 2
