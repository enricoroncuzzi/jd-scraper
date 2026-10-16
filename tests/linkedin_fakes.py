"""Shared fakes for LinkedIn collector tests: scripted HTTP and a recording pace."""
from contextlib import contextmanager
from unittest.mock import MagicMock

from src.linkedin.pace import Gate, Slot


def card_html(job_id: str, location: str = "Milan, Italy") -> str:
    return (
        f'<li><div class="base-search-card" data-entity-urn="urn:li:jobPosting:{job_id}">'
        f'<a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/job-{job_id}?trk=x"></a>'
        f'<h3 class="base-search-card__title">Job {job_id}</h3>'
        f'<h4 class="base-search-card__subtitle"><a>Company {job_id}</a></h4>'
        f'<span class="job-search-card__location">{location}</span>'
        f'<time datetime="2026-10-13">1 day ago</time></div></li>'
    )


def page_html(*job_ids: str) -> str:
    return "<ul>" + "".join(card_html(job_id) for job_id in job_ids) + "</ul>"


def ids(first: int, count: int) -> list[str]:
    return [str(3900000000 + first + offset) for offset in range(count)]


def response(status: int, text: str = "", headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.headers = headers or {}
    return resp


class ScriptedGet:
    """Returns the queued responses in order; records every request's params."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append(dict(params or {}))
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakePace:
    """Stands in for PaceBudget: grants every slot at once and records outcomes."""

    def __init__(self, wait: float = 0.0, gate: Gate | None = None):
        self.events = []
        self.retry_afters = []
        self.overdue_flags = []
        self.wait = wait
        self._gate = gate or Gate(open=True, reason="open", requests_today=0, cap=1400, window="weekday")

    def gate(self) -> Gate:
        return self._gate

    def seconds_until_slot(self) -> float:
        return self.wait

    @contextmanager
    def slot(self, kind, *, overdue=False):
        self.overdue_flags.append(overdue)
        slot = Slot()
        yield slot
        self.events.append((kind, slot.outcome))
        if slot.outcome == "throttled":
            self.retry_afters.append(slot.retry_after)
