def test_the_docs_no_longer_describe_the_breaker_or_the_taper():
    for path in ("README.md", "deploy/RUNBOOK.md"):
        text = open(path).read()
        assert "breaker" not in text.lower() and "taper" not in text.lower()
        assert "\u2014" not in text  # the em dash


def test_the_runbook_names_every_new_config_key():
    text = open("deploy/RUNBOOK.md").read()
    for key in ("start_spacing_seconds", "max_wait_seconds", "hour_multiplier", "calm_refusal_rate",
                "min_interval_hours", "max_interval_hours", "area_max_pages", "refused_pages_to_defer",
                "description_deadline_hours", "linkedin_blocked_after_open_hours",
                "linkedin_blocked_repeat_hours", "run_budget_seconds", "jds-resume-freehire.timer",
                "00..01,05..06,11..23:00/30:00", "area_cap"):
        assert key in text
