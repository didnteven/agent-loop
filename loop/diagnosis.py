"""Deterministic classification of trusted-check and worker diagnostics.

Classification happens *before* fingerprinting so that two failures with the
same underlying cause group together even when their text differs, and so that
two different causes with similar text stay apart. The classes are coarse on
purpose: they select a repair strategy, they do not explain the bug.

Nothing here calls a model. A model diagnosis is an optional advisory step that
runs after this one, never instead of it.
"""
import re

MISSING_DEPENDENCY = "missing_dependency"
TIMEOUT = "timeout"
ASSERTION = "assertion"
SYNTAX = "syntax"
PERMISSION = "permission"
UNKNOWN = "unknown"

_PATTERNS = (
    (MISSING_DEPENDENCY, re.compile(
        r"modulenotfounderror|no module named|importerror|cannot find module"
        r"|command not found|: not found|no such file or directory: '[^']*(bin|node_modules)"
        r"|unable to resolve dependency|could not find a version that satisfies")),
    (TIMEOUT, re.compile(r"\btimed out\b|\btimeout\b|worker timeout|deadline exceeded")),
    (SYNTAX, re.compile(r"syntaxerror|indentationerror|unexpected token|parse error")),
    (PERMISSION, re.compile(r"permission denied|operation not permitted|eacces")),
    (ASSERTION, re.compile(
        r"assertionerror|\bassert\b|test failed|failed:|\bfailures=\d|expected .* but")),
)


def classify(text):
    """Return one coarse failure class for a raw diagnostic."""
    lowered = str(text or "").lower()
    for name, pattern in _PATTERNS:
        if pattern.search(lowered):
            return name
    return UNKNOWN


def missing_dependency(text):
    """Name the dependency a diagnostic complains about, when it is stated."""
    for pattern in (r"no module named ['\"]([\w.\-]+)['\"]",
                    r"cannot find module ['\"]([\w./\-@]+)['\"]",
                    r"([\w.\-]+): command not found",
                    r"command not found: ([\w.\-]+)"):
        match = re.search(pattern, str(text or ""), re.I)
        if match:
            return match.group(1)
    return ""
