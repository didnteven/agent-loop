def clamp(value, low, high):
    if low > high:
        raise ValueError("low must be less than or equal to high")
    return max(low, min(value, high))
