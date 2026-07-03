import re
from src.tailor.generate import TailoredOutput

_NUM_RE = re.compile(r"\d[\d,\.]*")


def _numbers(text: str) -> set[str]:
    return {n.rstrip(".").rstrip(",") for n in _NUM_RE.findall(text)}


def check_claims(output: TailoredOutput, master_text: str) -> list[str]:
    master_numbers = _numbers(master_text)
    claim_text = " ".join(
        [output.summary, output.cover_letter]
        + [b for role in output.experience for b in role.bullets]
    )
    flagged = []
    for num in _numbers(claim_text):
        if num not in master_numbers:
            flagged.append(f"Unsupported number not in master CV: {num}")
    return flagged
