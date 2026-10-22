"""Recognise LinkedIn's challenge pages, conservatively.

Called only when the expected cards or description markup are missing, so a normal
job page that happens to use the word "challenge" cannot brake the pace. The markers
are LinkedIn path/form names and reCAPTCHA tokens, not ordinary words.
"""
CAPTCHA_MARKERS = (
    "/checkpoint/challenge",
    "challenge-form",
    "authwall",
    "security-verification",
    "complete the challenge",
    "g-recaptcha",
    "recaptcha",
    "captcha-internal",
)


def looks_like_captcha(html: str) -> bool:
    lowered = (html or "").lower()
    return any(marker in lowered for marker in CAPTCHA_MARKERS)
