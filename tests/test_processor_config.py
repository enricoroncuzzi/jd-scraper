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


def test_every_experimental_switch_ships_off():
    cfg = load_processor_config()
    assert cfg.structured_remote_enabled is False and cfg.merged_call_enabled is False
    assert not any(policy.title_prefilter for policy in cfg.policies.values())


def test_autoapply_settings_come_from_the_tier_configs_and_default_to_dry_run():
    policies = load_processor_config().policies
    assert all(p.autoapply_enabled and p.autoapply_dry_run for p in policies.values())
