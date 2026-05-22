from demo_math import add, subtract


def test_adds_numbers() -> None:
    assert add(2, 3) == 5


def test_subtracts_numbers() -> None:
    assert subtract(5, 3) == 2
