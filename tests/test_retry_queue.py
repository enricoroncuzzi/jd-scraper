import json
import os
from datetime import datetime, timedelta

from src.models import JobOffer
from src.retry_queue import (
    MAX_AGE_DAYS,
    QueueEntry,
    build_deferred,
    load_deferred,
    save_deferred,
)


def _offer(i: int, description: str = "a real fetched description") -> JobOffer:
    return JobOffer(
        id=i,
        title=f"Role {i}",
        company="Acme",
        location="Milan, Italy",
        link=f"https://li.com/{i}",
        description=description,
        description_status="ok",
        work_mode="remote",
        remote_verdict="unconfirmed",
        remote_reason="policy not explicit",
    )


def test_round_trip_preserves_the_offer_including_its_description(tmp_path):
    # The description was already paid for with a LinkedIn request (and a
    # 429-prone one); the next run must not have to fetch it again.
    path = str(tmp_path / "unscored_tier1.jsonl")
    offers = [_offer(0), _offer(1, description="another description")]

    save_deferred(path, build_deferred(offers, []))

    assert [e.offer for e in load_deferred(path)] == offers


def test_round_trip_preserves_the_verification_verdict(tmp_path):
    # Queued offers re-enter scoring directly, so the verdict the previous
    # run paid Groq for has to survive the round trip.
    path = str(tmp_path / "unscored_tier1.jsonl")

    save_deferred(path, build_deferred([_offer(0)], []))

    loaded = load_deferred(path)[0].offer
    assert loaded.remote_verdict == "unconfirmed"
    assert loaded.remote_reason == "policy not explicit"


def test_the_queue_is_plain_jsonl_one_entry_per_line(tmp_path):
    path = tmp_path / "unscored_tier1.jsonl"

    save_deferred(str(path), build_deferred([_offer(0), _offer(1)], []))

    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["offer"]["link"] for line in lines] == [
        "https://li.com/0",
        "https://li.com/1",
    ]
    assert "queued_at" in json.loads(lines[0])


def test_load_returns_nothing_when_there_is_no_queue_file(tmp_path):
    assert load_deferred(str(tmp_path / "unscored_tier1.jsonl")) == []


def test_entries_older_than_the_expiry_are_dropped(tmp_path):
    # A job posting goes stale, and an unbounded queue is its own bug.
    path = str(tmp_path / "unscored_tier1.jsonl")
    stale = QueueEntry(
        queued_at=datetime.now() - timedelta(days=MAX_AGE_DAYS, hours=1),
        offer=_offer(0),
    )
    fresh = QueueEntry(queued_at=datetime.now() - timedelta(hours=20), offer=_offer(1))

    save_deferred(path, [stale, fresh])

    assert [e.offer.link for e in load_deferred(path)] == ["https://li.com/1"]


def test_an_entry_just_inside_the_expiry_is_kept(tmp_path):
    path = str(tmp_path / "unscored_tier1.jsonl")
    entry = QueueEntry(
        queued_at=datetime.now() - timedelta(days=MAX_AGE_DAYS, minutes=-5),
        offer=_offer(0),
    )

    save_deferred(path, [entry])

    assert [e.offer.link for e in load_deferred(path)] == ["https://li.com/0"]


def test_unreadable_lines_are_skipped_without_failing_the_run(tmp_path, capsys):
    # The queue is a recovery aid: a half-written line must never take a run down.
    path = tmp_path / "unscored_tier1.jsonl"
    good = QueueEntry(queued_at=datetime.now(), offer=_offer(0))
    path.write_text(
        "truncated json\n"
        '{"queued_at": "2026-09-09T07:00:00", "offer": {"id": 3}}\n'
        f"{good.model_dump_json()}\n"
    )

    assert [e.offer.link for e in load_deferred(str(path))] == ["https://li.com/0"]
    assert "[queue]" in capsys.readouterr().out


def test_saving_nothing_removes_the_queue_file(tmp_path):
    path = str(tmp_path / "unscored_tier1.jsonl")
    save_deferred(path, build_deferred([_offer(0)], []))

    save_deferred(path, [])

    assert load_deferred(path) == []
    assert not os.path.exists(path)


def test_saving_nothing_never_creates_directories(tmp_path):
    # main.handler runs on configs whose dedup log lives somewhere the test
    # process may not be able to create; an empty queue must stay a no-op.
    path = str(tmp_path / "nope" / "unscored_tier1.jsonl")

    save_deferred(path, [])

    assert not os.path.exists(os.path.dirname(path))


def test_an_offer_deferred_again_keeps_its_original_timestamp():
    # Expiry counts from the first deferral, otherwise a queue that keeps
    # failing would carry the same stale posting forever.
    now = datetime(2026, 9, 10, 7, 0, 0)
    original = now - timedelta(days=2)
    previous = [QueueEntry(queued_at=original, offer=_offer(0))]

    entries = build_deferred([_offer(0)], previous, now=now)

    assert entries[0].queued_at == original


def test_a_first_time_deferral_is_stamped_with_now():
    now = datetime(2026, 9, 10, 7, 0, 0)

    entries = build_deferred([_offer(3)], [], now=now)

    assert entries[0].queued_at == now
    assert entries[0].offer.link == "https://li.com/3"
