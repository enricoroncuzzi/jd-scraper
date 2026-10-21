from src.processor.config import load_processor_config


def test_the_shipped_config_loads_the_four_tier_policies():
    cfg = load_processor_config()
    assert sorted(cfg.policies) == [1, 2, 3, 4]
    assert (cfg.remote_check_batch_size, cfg.scoring_batch_size, cfg.flush_after_minutes) == (8, 5, 30)


def test_remote_rules_match_the_tier_configs():
    policies = load_processor_config().policies
    assert [policies[t].remote_check_enabled for t in (1, 2, 3, 4)] == [True, False, True, True]
    assert [policies[t].require_italy_eligibility for t in (1, 3, 4)] == [False, True, True]
    assert all(policies[t].threshold == 8 for t in (1, 2, 3, 4))


def test_tiers_one_three_and_four_share_a_scoring_batch_but_tier_two_does_not():
    policies = load_processor_config().policies
    assert policies[1].scoring_key == policies[3].scoring_key == policies[4].scoring_key
    assert policies[2].scoring_key != policies[1].scoring_key


def test_the_shipped_switches_are_the_rule_pre_pass_and_the_reject_hint_shortcut():
    cfg = load_processor_config()
    # 2026-10-18: phrase rules and the hybrid/on-site source hint are on; the
    # merged call stays off.
    assert cfg.remote_rules_enabled is True and cfg.structured_remote_enabled is True
    assert cfg.merged_call_enabled is False
    assert all(policy.title_prefilter for policy in cfg.policies.values())


def test_the_title_prefilter_uses_the_strict_keywords_and_no_broad_ones():
    for policy in load_processor_config().policies.values():
        keywords = [keyword.lower() for keyword in policy.exclude_keywords]
        for strict in ("principal", "staff", "vp", "director"):
            assert strict in keywords
        for broad in ("senior", "lead", "head of"):
            assert broad not in keywords


def test_autoapply_settings_come_from_the_tier_configs_and_default_to_dry_run():
    policies = load_processor_config().policies
    assert all(p.autoapply_enabled and p.autoapply_dry_run for p in policies.values())


def test_the_description_stage_has_a_time_budget_per_tick():
    assert load_processor_config().description_seconds_per_tick == 120


def test_the_shipped_config_turns_the_order_on_with_a_quota_of_four():
    cfg = load_processor_config()
    assert (cfg.fetch_order.enabled, cfg.fetch_order.quota_one_in) == (True, 4)
    assert cfg.fetch_order.backfill_per_tick == 500
    assert [c.name for c in cfg.fetch_order.order_for(1).classes] == [
        "ai_country", "ai_city", "other_country", "other_city"]
    assert [c.name for c in cfg.fetch_order.order_for(3).classes] == [
        "ai_country", "ai_city", "other_country", "other_city"]
    assert [c.name for c in cfg.fetch_order.order_for(4).classes] == [
        "ai_country", "ai_city", "other_country", "other_city"]
    assert [c.name for c in cfg.fetch_order.order_for(2).classes] == ["ai", "other"]


def test_the_occasional_presence_guard_is_on_and_configurable(tmp_path):
    import json

    assert load_processor_config().occasional_presence_guard is True
    data = json.load(open("config/processor.json"))
    data["remote_rules"]["occasional_presence"] = False
    path = tmp_path / "processor.json"
    path.write_text(json.dumps(data))
    assert load_processor_config(str(path)).occasional_presence_guard is False


def test_the_cloud_hybrid_guard_is_on_and_configurable(tmp_path):
    import json

    assert load_processor_config().cloud_hybrid_guard is True
    data = json.load(open("config/processor.json"))
    data["remote_rules"]["cloud_hybrid"] = False
    path = tmp_path / "processor.json"
    path.write_text(json.dumps(data))
    assert load_processor_config(str(path)).cloud_hybrid_guard is False


def test_the_remote_hint_advice_is_on_and_configurable(tmp_path):
    import json

    assert load_processor_config().remote_hint_advice is True
    data = json.load(open("config/processor.json"))
    data["remote_hint"]["advised_when_silent"] = False
    path = tmp_path / "processor.json"
    path.write_text(json.dumps(data))
    assert load_processor_config(str(path)).remote_hint_advice is False


def test_the_explicit_full_remote_override_is_on_and_configurable(tmp_path):
    import json

    assert load_processor_config().explicit_remote_wins is True
    data = json.load(open("config/processor.json"))
    data["structured_remote"]["explicit_remote_wins"] = False
    path = tmp_path / "processor.json"
    path.write_text(json.dumps(data))
    assert load_processor_config(str(path)).explicit_remote_wins is False


def test_the_remote_hint_never_reject_guard_is_on_and_configurable(tmp_path):
    import json

    assert load_processor_config().remote_hint_never_reject is True
    data = json.load(open("config/processor.json"))
    data["remote_hint"]["never_reject_on_occasional"] = False
    path = tmp_path / "processor.json"
    path.write_text(json.dumps(data))
    assert load_processor_config(str(path)).remote_hint_never_reject is False


def test_the_occasional_presence_prompt_is_on_and_configurable(tmp_path):
    import json

    assert load_processor_config().occasional_presence_prompt is True
    data = json.load(open("config/processor.json"))
    data["remote_verifier"]["occasional_presence_prompt"] = False
    path = tmp_path / "processor.json"
    path.write_text(json.dumps(data))
    assert load_processor_config(str(path)).occasional_presence_prompt is False
