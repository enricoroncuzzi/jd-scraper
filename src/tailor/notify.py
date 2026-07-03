import subprocess


def _notify_script(title: str, message: str) -> str:
    t = title.replace('"', '\\"')
    m = message.replace('"', '\\"')
    return f'display notification "{m}" with title "{t}"'


def _open_args(path: str) -> list[str]:
    return ["open", path]


def notify(title: str, message: str) -> None:
    subprocess.run(["osascript", "-e", _notify_script(title, message)], check=False)


def reveal(path: str) -> None:
    subprocess.run(_open_args(path), check=False)
