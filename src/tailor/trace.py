"""`TAILOR_TRACE.json`: what made a tailor package.

Every package the tailor writes carries this file next to `CLAIMS_REVIEW.txt`, on success and
on every gate stop. It names the inputs by hash, never by path, so master drift between the
Mac and the server is detectable to the byte without any text leaving the machine. It holds
no contact details either: the letterhead is not in it. Fixed letter paragraphs are, with
`{company}` filled in, because they are the sent letter's own text.

Schema 1, keys in this order: `schema`, `made_at`, `origin` (`auto` for the server's
auto-packaging, `manual` for a Mac re-tailor), `status` (`PASS` or `FAIL`), `stop` (the halt
reason without its `HALTED: ` prefix, or null), `cv_master_sha256`, `cl_master_sha256`,
`css_sha256`, `prompt_version`, `model_requested`, `model_answered`, `fit_px`, `selection`
(`skill_order`, `proof_id`, `hook`, `bridge`), `letter_parts`.

`made_at` carries the offset of the machine that made the package; readers parse the offset
and never assume a zone. The trace is advisory: a trace that cannot be written never changes
the packaging outcome, and the snapshot builder treats a package without one as legacy.
"""
import hashlib
import json
import os
from datetime import datetime

from src.tailor.cl_master import COMPANY_TOKEN, CoverLetterMaster
from src.tailor.generate import Generation

TRACE_FILENAME = "TAILOR_TRACE.json"
TRACE_SCHEMA = 1

_ORIGINS = ("auto", "manual")
_STATUSES = ("PASS", "FAIL")


def file_sha256(path: str) -> str | None:
    """Hex sha256 of the file's bytes; None when the path is empty or the file is absent.

    `tailor.run` tolerates a missing or empty CSS path, so the hash has to as well.
    """
    if not path or not os.path.exists(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def letter_parts(master: CoverLetterMaster, company: str, proof_id: str) -> list[dict]:
    """One entry per master part, in order, mirroring `CoverLetterMaster.compose`."""
    parts = []
    for part in master.parts:
        if part.kind == "fixed":
            parts.append({
                "kind": "fixed",
                "text": part.text.replace(COMPANY_TOKEN, company.strip()),
            })
        elif part.kind == "proof":
            parts.append({"kind": "proof", "proof_id": proof_id})
        else:
            parts.append({"kind": part.kind})
    return parts


def build_trace(*, origin: str, status: str, stop: str | None,
                cv_master_path: str, cl_master_path: str, css_path: str,
                prompt_version: str, generation: Generation, skill_order: list[str],
                fit_px: float | None, letter: list[dict],
                now: datetime | None = None) -> dict:
    """The trace dict for one package. A bad `origin` or `status` is a programming error."""
    if origin not in _ORIGINS:
        raise ValueError(f"unknown trace origin: {origin!r}")
    if status not in _STATUSES:
        raise ValueError(f"unknown trace status: {status!r}")
    sel = generation.selection
    return {
        "schema": TRACE_SCHEMA,
        "made_at": (now or datetime.now()).astimezone().isoformat(timespec="seconds"),
        "origin": origin,
        "status": status,
        "stop": stop,
        "cv_master_sha256": file_sha256(cv_master_path),
        "cl_master_sha256": file_sha256(cl_master_path),
        "css_sha256": file_sha256(css_path),
        "prompt_version": prompt_version,
        "model_requested": generation.model_requested,
        "model_answered": generation.model_answered,
        "fit_px": fit_px,
        "selection": {
            "skill_order": list(skill_order),
            "proof_id": sel.cover_letter.proof_id,
            "hook": sel.cover_letter.hook,
            "bridge": sel.cover_letter.bridge,
        },
        "letter_parts": letter,
    }


def write_trace(directory: str, trace: dict) -> str:
    """Write the trace atomically and return the path written.

    A temporary file in the same folder followed by `os.replace` means a crash mid-write can
    never leave half a JSON file that a reader would report as unparsed forever.
    """
    final = os.path.join(directory, TRACE_FILENAME)
    tmp = final + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(trace, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, final)
    return final
