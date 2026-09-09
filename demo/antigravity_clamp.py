def clamp(value, low, high):
    """Bound value to the closed interval [low, high].

    Raises:
        ValueError: If low > high.
    """
    if low > high:
        raise ValueError("low cannot be greater than high")
    if value < low:
        return low
    if value > high:
        return high
    return value
