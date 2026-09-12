"""Independent acceptance check shared by all three real providers.

Each provider gets a thin wrapper (``verify_<provider>_clamp.py``) that names its
own candidate file, rather than the supervisor passing that path as an argument.
That is deliberate: a task's allowlisted files may never appear in its own check
argv, because the supervisor cannot tell a file the check *judges* from a file
the check *is*, and the safe reading of an ambiguous case is to reject the plan.
"""
import importlib.util
import sys

CASES = [(5, 0, 10, 5), (-1, 0, 10, 0), (11, 0, 10, 10),
         (0, 0, 10, 0), (10, 0, 10, 10), (-5, -10, -2, -5),
         (-20, -10, -2, -10), (1.25, 1.0, 1.5, 1.25),
         (1.75, 1.0, 1.5, 1.5), (12, 3, 3, 3)]


def verify(path):
    spec = importlib.util.spec_from_file_location("candidate", path)
    candidate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(candidate)
    clamp = candidate.clamp
    for value, low, high, expected in CASES:
        actual = clamp(value, low, high)
        assert actual == expected, (value, low, high, actual, expected)
    try:
        clamp(5, 10, 0)
    except ValueError:
        pass
    else:
        raise AssertionError("Reversed bounds must raise ValueError")
    print("PASS: 11 acceptance cases")


if __name__ == "__main__":
    verify(sys.argv[1])
