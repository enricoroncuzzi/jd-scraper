"""Replay a fixed sample of past jobs through a candidate language-model configuration.
See src/processor/experiment_cli.py for usage."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

from src.processor.experiment_cli import main  # noqa: E402

if __name__ == "__main__":
    load_dotenv()
    sys.exit(main(sys.argv[1:]))
