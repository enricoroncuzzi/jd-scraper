from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE_CL_MASTER_PATH = FIXTURES / "CL_master.example.md"
EXAMPLE_CL_MASTER = EXAMPLE_CL_MASTER_PATH.read_text()


def write_cl_master(tmp_path, text=None) -> str:
    """A valid letter master on disk; the shipped example by default."""
    path = tmp_path / "CL_master.md"
    path.write_text(EXAMPLE_CL_MASTER if text is None else text)
    return str(path)
