"""The free-model pins each stage hands OpenRouter, and the slugs that are gone.

The 2026-10-21 free-model eval (report section 6) replaced the dead Nemotron
Super primary and reordered the chains. Each stage keeps its own constants, so
the pins are asserted per file here rather than through a shared default: one
stage's pin change must not silently retarget another.
"""
import subprocess
from pathlib import Path

import pytest

from src import remote_verifier as rv
from src import scorer
from src.tailor import generate

ULTRA = "nvidia/nemotron-3-ultra-550b-a55b:free"
VL = "nvidia/nemotron-3.5-super-vl:free"
DOTS = "dots-studio/dots-3-note-preview:free"
LFM = "liquid/lfm-2.5-2.6b:free"

# The slugs the eval measured as unusable: Nemotron Super 404s on every call
# since 2026-10-20 10:13 Rome, and apodex is not in OpenRouter's model list.
REMOVED = (
    "nvidia/nemotron-3-super-120b-a12b:free",
    "apodex/apodex-1.1-mini:free",
)

REPO_ROOT = Path(__file__).resolve().parents[1]

PINS = {
    "scoring": (scorer._OPENROUTER_MODEL, list(scorer._OPENROUTER_FALLBACK_MODELS)),
    "verification": (rv._OPENROUTER_MODEL, list(rv._OPENROUTER_FALLBACK_MODELS)),
    "tailoring": (generate._OPENROUTER_MODEL, list(generate._OPENROUTER_FALLBACK_MODELS)),
}


def test_each_stage_pins_the_approved_primary_and_fallback_order():
    assert PINS == {
        "scoring": (ULTRA, [VL, DOTS, LFM]),
        "verification": (VL, [ULTRA, DOTS, LFM]),
        "tailoring": (ULTRA, [DOTS, LFM, VL]),
    }


@pytest.mark.parametrize("stage", sorted(PINS))
def test_no_stage_passes_more_than_three_fallbacks(stage):
    """OpenRouter 400s a `models` array above 3 entries."""
    primary, fallbacks = PINS[stage]
    assert len(fallbacks) <= 3
    assert primary not in fallbacks
    assert len(set(fallbacks)) == len(fallbacks)


def test_the_removed_models_are_named_nowhere_in_the_tracked_tree():
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    needles = REMOVED
    offenders = []
    for name in tracked:
        if name == "tests/test_model_pins.py":  # holds the slugs as needles
            continue
        path = REPO_ROOT / name
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for needle in needles:
            if needle in text:
                offenders.append(f"{name}: {needle}")
    assert offenders == []
