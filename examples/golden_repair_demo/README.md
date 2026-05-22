# Golden Repair Demo Repository

This fixture is intentionally small and broken. It gives Kestrel a deterministic first repair target for the product golden workflow:

1. Connect/select this repository.
2. Run `python -m pytest -q` and observe the failing subtraction test.
3. Diagnose the bug in `src/demo_math/calculator.py`.
4. Apply the equivalent of `expected_fix.patch`.
5. Re-run `python -m pytest -q` and verify the fixture passes.
6. Produce a repair review artifact before any commit.

The fixture is not imported by Kestrel's main test suite directly; tests copy it to a temporary directory before validating the broken-then-fixed flow.
