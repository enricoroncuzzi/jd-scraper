import pytest

from tests.tailor_helpers import EXAMPLE_CL_MASTER, write_cl_master


def test_the_shipped_example_parses():
    from src.tailor.cl_master import parse_cl_master
    parse_cl_master(EXAMPLE_CL_MASTER)


def test_letterhead_and_letter_parts_in_order():
    from src.tailor.cl_master import parse_cl_master
    m = parse_cl_master(EXAMPLE_CL_MASTER)
    assert m.letterhead.name == "Alex Doe"
    assert m.letterhead.title == "COVER LETTER"
    assert m.letterhead.contact == (
        "alex.doe@example.com", "(+00) 000 000 0000", "linkedin.com/in/example", "Italy")
    assert [p.kind for p in m.parts] == ["fixed", "hook", "bridge", "proof", "fixed"]
    assert m.parts[0].text == "Dear {company} team,"
    assert m.parts[-1].text == (
        "I am based in Italy. I would be glad to discuss how my background fits your team.")
    assert list(m.proof_pool) == ["lp.example.0"]


def test_each_rule_break_names_its_line(tmp_path):
    from src.tailor.cl_master import load_cl_master

    def broken(old, new):
        return write_cl_master(tmp_path, EXAMPLE_CL_MASTER.replace(old, new, 1))

    with pytest.raises(ValueError, match=r"line \d+: slot \{\{proof\}\} comes before"):
        load_cl_master(broken("{{bridge}}\n\n{{proof}}", "{{proof}}\n\n{{bridge}}"))
    with pytest.raises(ValueError, match=r"line \d+: .*slot must be alone in its paragraph"):
        load_cl_master(broken("{{hook}}", "{{hook}} and more"))
    with pytest.raises(ValueError, match=r"line \d+: unknown slot \{\{Proof\}\}"):
        load_cl_master(broken("{{proof}}", "{{Proof}}"))
    with pytest.raises(ValueError, match=r"line \d+: unknown brace group \{companY\}"):
        load_cl_master(broken("Dear {company} team,", "Dear {companY} team,"))
    with pytest.raises(ValueError, match=r"line \d+: unbalanced brace"):
        load_cl_master(broken("Dear {company} team,", "Dear {company team,"))
    with pytest.raises(ValueError, match=r"line \d+: unbalanced brace"):
        load_cl_master(broken("Dear {company} team,", "Dear company} team,"))
    with pytest.raises(ValueError, match=r"line \d+: unterminated HTML comment"):
        load_cl_master(broken("{{hook}}", "<!-- note\n{{hook}}"))
    with pytest.raises(ValueError, match=r"line \d+: missing slot \{\{bridge\}\}"):
        load_cl_master(broken("{{bridge}}\n\n", ""))
    with pytest.raises(ValueError, match=r"line \d+: duplicate proof-pool id 'lp.example.0'"):
        load_cl_master(broken("- lp.example.0 ::", "- lp.example.0 :: more\n- lp.example.0 ::"))
    with pytest.raises(ValueError, match=r"line \d+: unknown letterhead key 'phone'"):
        load_cl_master(broken("- title:", "- phone: 1\n- title:"))
    with pytest.raises(ValueError, match="missing section '## Proof Pool'"):
        load_cl_master(broken("## Proof Pool", "## Proofs"))


def test_content_rules_reject_a_dash_and_a_banned_phrase(tmp_path):
    from src.tailor.cl_master import load_cl_master
    with pytest.raises(ValueError, match=r"line \d+: dash used"):
        load_cl_master(write_cl_master(
            tmp_path, EXAMPLE_CL_MASTER.replace(
                "Dear {company} team,", "Dear {company} team - hello")))
    with pytest.raises(ValueError, match=r"line \d+: banned phrase: 'excited to'"):
        load_cl_master(write_cl_master(
            tmp_path, EXAMPLE_CL_MASTER.replace("I am based in Italy.", "Excited to join.")))


def test_wrapped_lines_join_with_a_single_space(tmp_path):
    from src.tailor.cl_master import load_cl_master
    m = load_cl_master(write_cl_master(tmp_path, EXAMPLE_CL_MASTER.replace(
        "I am based in Italy. I would be glad",
        "I am based in Italy.\nI would be glad")))
    assert m.parts[-1].text == (
        "I am based in Italy. I would be glad to discuss how my background fits your team.")


def test_comment_lines_keep_error_line_numbers(tmp_path):
    from src.tailor.cl_master import load_cl_master
    text = EXAMPLE_CL_MASTER.replace(
        "{{hook}}", "<!-- a\nmulti line\nnote -->\n{{hook}}").replace("{{proof}}", "{{Proof}}")
    n = text.splitlines().index("{{Proof}}") + 1
    with pytest.raises(ValueError, match=rf"line {n}: unknown slot"):
        load_cl_master(write_cl_master(tmp_path, text))


def test_the_pool_can_be_swapped_into_a_copy_of_the_canonical_cv(tmp_path):
    from src.tailor.cl_master import parse_cl_master
    from src.tailor.cv_master import load_canonical

    cv = tmp_path / "CV_master.md"
    cv.write_text(
        "## Summary\ns\n\n## Skills\n\n**Languages:** Python\n\n## Experience\n\n"
        "**AI Engineer**\n  : **Hey-Movo**\n\n- Built a coordinator agent.\n")
    canonical = load_canonical(str(cv))
    moved = canonical.with_letter_proof_pool({"lp.example.0": "Built one."})
    assert moved.letter_proof_pool == {"lp.example.0": "Built one."}
    assert canonical.letter_proof_pool == {}          # the original is untouched
    assert moved.bullet_text("lp.example.0") == "Built one."
    assert parse_cl_master(EXAMPLE_CL_MASTER).proof_pool["lp.example.0"].startswith(
        "At Example Labs")


def test_the_voice_check_runs_on_the_fixed_text_when_it_exists(tmp_path, monkeypatch):
    import src.tailor.cl_master as cl_master
    monkeypatch.setattr(
        cl_master, "_additional_text_checks",
        lambda: [lambda text: ["third person: 'he'"] if "he says" in text.lower() else []])
    with pytest.raises(ValueError, match=r"line \d+: third person"):
        cl_master.load_cl_master(write_cl_master(
            tmp_path, EXAMPLE_CL_MASTER.replace("I am based in", "He says I am based in")))


def test_the_example_master_loads_whatever_the_voice_check_state(tmp_path):
    # Green with the voice fix absent and green once check_voice exists: the fixture's fixed
    # text and pool sentence are first person.
    from src.tailor.cl_master import load_cl_master
    load_cl_master(write_cl_master(tmp_path))
