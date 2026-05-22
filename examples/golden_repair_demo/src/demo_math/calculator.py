"""Tiny arithmetic module with one seeded bug for repair demos."""


def add(left: int, right: int) -> int:
    return left + right


def subtract(left: int, right: int) -> int:
    # Seeded defect: subtraction should subtract the right operand.
    return left + right
