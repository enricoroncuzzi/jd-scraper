from src.tailor.notify import _notify_script, _open_args


def test_notify_script_basic():
    assert _notify_script("CV tailored", "logicalis_spain") == (
        'display notification "logicalis_spain" with title "CV tailored"'
    )


def test_notify_script_escapes_double_quotes():
    script = _notify_script('Say "hi"', 'a "b" c')
    assert '\\"hi\\"' in script
    assert '\\"b\\"' in script


def test_notify_script_handles_newlines_and_backslashes():
    script = _notify_script("t", "line1\nline2\\path\rend")
    # no raw newline/carriage-return should survive into the AppleScript literal
    assert "\n" not in script.split(" with title", 1)[0].split('"', 1)[1]
    assert "\r" not in script
    # backslash is escaped (doubled) so it doesn't start an AppleScript escape
    assert "\\\\path" in script


def test_open_args():
    assert _open_args("/Users/x/tailored/acme") == ["open", "/Users/x/tailored/acme"]
