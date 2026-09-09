"""Independent acceptance check shared by all three real providers."""
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("candidate", sys.argv[1])
candidate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(candidate)
clamp = candidate.clamp
cases = [(5, 0, 10, 5), (-1, 0, 10, 0), (11, 0, 10, 10),
         (0, 0, 10, 0), (10, 0, 10, 10), (-5, -10, -2, -5),
         (-20, -10, -2, -10), (1.25, 1.0, 1.5, 1.25),
         (1.75, 1.0, 1.5, 1.5), (12, 3, 3, 3)]
for value, low, high, expected in cases:
    actual = clamp(value, low, high)
    assert actual == expected, (value, low, high, actual, expected)
try:
    clamp(5, 10, 0)
except ValueError:
    pass
else:
    raise AssertionError("Reversed bounds must raise ValueError")
print("PASS: 11 acceptance cases")
