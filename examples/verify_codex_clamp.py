"""Trusted acceptance check for the codex clamp task. Names its own subject."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from verify_clamp import verify

verify("demo/codex_clamp.py")
