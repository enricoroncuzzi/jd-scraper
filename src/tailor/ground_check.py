import re
from src.tailor.generate import TailoredOutput

_NUM_RE = re.compile(r"\d[\d,\.]*")


def _normalize(n: str) -> str:
    n = n.rstrip(".").rstrip(",")
    n = n.replace(",", "")
    if "." in n:
        n = n.rstrip("0").rstrip(".")
    return n


def _numbers(text: str) -> set[str]:
    return {_normalize(n) for n in _NUM_RE.findall(text)}


def check_claims(output: TailoredOutput, master_text: str, jd_text: str = "") -> list[str]:
    master_numbers = _numbers(master_text)
    allowed_for_cover = master_numbers | _numbers(jd_text)

    strict_text = " ".join(
        [output.summary]
        + [b for role in output.experience for b in role.bullets]
    )
    flagged = []
    for num in _numbers(strict_text):
        if num not in master_numbers:
            flagged.append(f"Unsupported number not in master CV: {num}")
    for num in _numbers(output.cover_letter):
        if num not in allowed_for_cover:
            flagged.append(f"Unsupported number not in master CV: {num}")
    return flagged
