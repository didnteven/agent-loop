"""Shared test helpers."""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch


def isolate_registry(testcase):
    """Keep account-global accounting out of the developer's real registry.

    The registry deliberately lives outside any target repository: writing it
    inside one would make an otherwise clean target dirty.
    """
    directory = tempfile.TemporaryDirectory()
    testcase.addCleanup(directory.cleanup)
    patcher = patch.dict(os.environ, {
        "AGENT_LOOP_REGISTRY": str(Path(directory.name) / "registry.sqlite"),
        "AGENT_LOOP_ACCOUNT": "test-account"})
    patcher.start()
    testcase.addCleanup(patcher.stop)
    return Path(directory.name)
