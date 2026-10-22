from src.linkedin.captcha import looks_like_captcha


def test_a_captcha_challenge_page_is_recognised():
    assert looks_like_captcha("<html>/checkpoint/challenge</html>")
    assert looks_like_captcha("Please complete the challenge to continue")
    assert looks_like_captcha("<div class='g-recaptcha'></div>")


def test_a_normal_job_page_is_not_a_captcha():
    assert not looks_like_captcha(
        "<div class='show-more-less-html__markup'>We challenge you</div>")
    assert not looks_like_captcha("<h1>Checkpoint Firewall Engineer</h1>")
    assert not looks_like_captcha("<p>Detect fraud and unusual activity</p>")
    assert not looks_like_captcha("")
