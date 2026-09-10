def clamp(value, low, high):
    """Bound value to the closed interval [low, high], preserving values inside it.

    Raises:
        ValueError: If low > high.
    """
    if low > high:
        raise ValueError(f"low ({low}) must not be greater than high ({high})")
    if value < low:
        return low
    if value > high:
        return high
    return value
