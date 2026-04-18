# Test fixtures

This directory holds the real-data fixtures used by
[`tests/test_fit_fixer.py`](../tests/test_fit_fixer.py). The fit files
themselves are **not committed** to the repository because they contain
real GPS traces (personal cycling routes, timestamps, etc.) that the
project owner prefers to keep private.

The test module will **automatically skip** its 3 regression tests when
these files are missing, so `uv run pytest` still passes on a fresh
clone — it will just report `skipped` instead of `passed` for the
fixture-based tests.

## Expected files

To run the full regression suite locally, place two fit files here:

| File | Description |
| --- | --- |
| `MAGENE_C506_bias.fit`    | A GCJ-02 biased fit (e.g. exported from the Onelap app). |
| `MAGENE_C506_correct.fit` | A ground-truth WGS-84 fit for the same route (e.g. pulled directly from a head unit like a Magene C506). |

Both files must cover roughly the same route (independent rides are
fine — the test does not require point-for-point matching). File names
are referenced by constant in `tests/test_fit_fixer.py`; rename those
constants if you want to use different names.

## What the regression test validates

See [contexts/phase1-offline-script.md](../contexts/phase1-offline-script.md)
section 3.3 and [`tests/test_fit_fixer.py`](../tests/test_fit_fixer.py)
docstring for the full testing philosophy. In short: we verify that
**GCJ-02's systematic offset has been eliminated**, not that the fixed
track matches the reference point-by-point (two independent rides
naturally differ by GPS noise + lane differences).
