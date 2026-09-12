"""A model-aware bound on the whole prompt, not just on each context file.

The old 30,000-character cap applied per context file, so a task with several
files, a long failure tail and a journal could still assemble an unbounded
prompt. This module allocates one budget across all sections in priority order
and reserves room for the model's own output.

Sections are trimmed, never silently dropped: a trimmed section says so, so a
worker can tell the difference between "this file is short" and "you are seeing
part of this file".
"""

# Characters, not tokens: the CLIs do not expose a tokenizer, and a character
# budget that is deliberately conservative is honest about being an estimate.
DEFAULT_CONTEXT_CHARACTERS = 120000
OUTPUT_RESERVE_FRACTION = 0.25
TRIM_NOTICE = "\n[trimmed to fit the context budget]\n"


def model_budget(policy=None, model=None, override=None):
    """Total prompt characters allowed for one invocation."""
    if isinstance(override, int) and override > 0:
        return override
    budgets = (policy or {}).get("context_characters", {})
    if isinstance(budgets, int) and budgets > 0:
        return budgets
    if isinstance(budgets, dict):
        for key in (model, "default"):
            value = budgets.get(key)
            if isinstance(value, int) and value > 0:
                return value
    return DEFAULT_CONTEXT_CHARACTERS


def fit(sections, budget):
    """Assemble prioritized ``(name, text)`` sections within one budget.

    Earlier sections are more important and are served first; what remains is
    shared by the rest. A section that cannot fit at all is omitted with a note
    rather than being cut to a misleading fragment.
    """
    allowance = max(1, int(budget * (1 - OUTPUT_RESERVE_FRACTION)))
    parts, used, omitted = [], 0, []
    for name, text in sections:
        text = "" if text is None else str(text)
        if not text:
            continue
        remaining = allowance - used
        if remaining <= len(TRIM_NOTICE) + 200:
            omitted.append(name)
            continue
        if len(text) <= remaining:
            parts.append(text)
            used += len(text)
            continue
        parts.append(text[:remaining - len(TRIM_NOTICE)] + TRIM_NOTICE)
        used = allowance
    if omitted:
        parts.append("\n[omitted for context budget: " + ", ".join(omitted) + "]\n")
    return "".join(parts)
