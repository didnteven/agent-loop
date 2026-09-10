def clamp(value, low, high):
    """Bound value to the closed interval [low, high].
    
    Args:
        value: The value to clamp
        low: The lower bound (inclusive)
        high: The upper bound (inclusive)
    
    Returns:
        The clamped value
    
    Raises:
        ValueError: If low > high
    """
    if low > high:
        raise ValueError("low must be less than or equal to high")
    return max(low, min(value, high))
