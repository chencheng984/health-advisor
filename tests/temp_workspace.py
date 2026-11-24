"""Keep test fixtures inside the writable workspace on confined hosts."""

from pathlib import Path
from tempfile import TemporaryDirectory


def temporary_directory():
    root = Path(__file__).resolve().parents[1] / "_tmp"
    root.mkdir(exist_ok=True)
    return TemporaryDirectory(dir=root, ignore_cleanup_errors=True)
