def classify_review_burden(value):
    """Classify review burden.

    Args:
        value: Non-negative numeric value.

    Returns:
        'none', 'minor', 'moderate', or 'major'.

    Raises:
        ValueError: If value is negative.
    """
    if value < 0:
        raise ValueError("value must be non-negative")
    if value == 0:
        return 'none'
    elif value <= 0.25:
        return 'minor'
    elif value <= 0.6:
        return 'moderate'
    else:
        return 'major'
