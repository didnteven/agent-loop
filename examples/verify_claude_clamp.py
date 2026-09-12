"""Trusted acceptance check for the claude clamp task. Names its own subject."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from verify_clamp import verify

verify("demo/claude_clamp.py")
