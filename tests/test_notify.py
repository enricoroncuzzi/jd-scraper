from src.tailor.notify import _notify_script, _open_args


def test_notify_script_basic():
    assert _notify_script("CV tailored", "logicalis_spain") == (
        'display notification "logicalis_spain" with title "CV tailored"'
    )


def test_notify_script_escapes_double_quotes():
    script = _notify_script('Say "hi"', 'a "b" c')
    assert '\\"hi\\"' in script
    assert '\\"b\\"' in script


def test_open_args():
    assert _open_args("/Users/x/tailored/acme") == ["open", "/Users/x/tailored/acme"]
