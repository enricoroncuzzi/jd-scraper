from src import remote_verdicts as rv


def test_legacy_unconfirmed_reads_as_not_stated():
    assert rv.normalise("unconfirmed") == rv.NOT_STATED
    assert rv.is_not_stated("unconfirmed") is True
    assert rv.is_not_stated(None) is False  # a row the check never visited


def test_a_missing_verdict_reads_as_not_checked():
    assert rv.normalise(None) == rv.NOT_CHECKED
    assert rv.normalise("") == rv.NOT_CHECKED
    assert rv.is_confirmed_or_unchecked(None) is True


def test_alerting_is_only_withheld_for_not_stated_and_rejected():
    assert rv.may_alert("confirmed") is True
    assert rv.may_alert("advised") is True
    assert rv.may_alert("not_checked") is True
    assert rv.may_alert("not_stated") is False
    assert rv.may_alert("unconfirmed") is False
    assert rv.may_alert("rejected") is False


def test_auto_packaging_stays_on_confirmed_and_tier_two_only():
    assert rv.may_autopackage("confirmed") is True
    assert rv.may_autopackage("not_checked") is True
    assert rv.may_autopackage("advised") is False
    assert rv.may_autopackage("not_stated") is False
    assert rv.may_autopackage("unconfirmed") is False
    assert rv.may_autopackage("rejected") is False


def test_a_legacy_unconfirmed_is_never_packaged():
    assert rv.may_autopackage("unconfirmed") is False


def test_section_labels_name_the_three_delivered_outcomes():
    assert rv.section_label("confirmed") == "Confirmed full-remote"
    assert rv.section_label("advised") == "Remote, eligibility to check"
    assert rv.section_label("not_stated") == "Work mode not stated"
    assert rv.section_label("unconfirmed") == "Work mode not stated"


def test_advice_prefers_what_the_stage_stored():
    assert rv.advice_line("advised", "remote, limited to the UK - check eligibility") == \
        "remote, limited to the UK - check eligibility"
    assert rv.advice_line("advised", "", "Remote (UK) only.") == \
        "remote, limited to the UK - check eligibility"
    assert rv.advice_line("advised", "", "") == "remote, eligibility to check"
    assert rv.advice_line("confirmed", "should not show") == ""


def test_country_detection_handles_short_and_ambiguous_forms():
    assert rv.country_in("Remote (UK) only") == "the UK"
    assert rv.country_in("must be based in the UK") == "the UK"
    assert rv.country_in("contact us for details") is None  # "us", the pronoun
    assert rv.country_in("Remote from France") == "France"
    assert rv.country_in("Berlin, Germany") == "Germany"


def test_the_english_pronoun_us_is_never_the_united_states():
    for text in ("Many of us work remotely", "Talk to us", "Hear from us", "ABOUT US",
                 "JOIN US TODAY"):
        assert rv.country_in(text) is None, text
    for text in ("Remote (US)", "must live in the US", "within the US", "US-based team",
                 "Open to US residents"):
        assert rv.country_in(text) == "the US", text
