"""Throwaway: a vdsm test that always fails, to check that a failed vdsm job sets CI Failed.

Do not merge.
"""

import pytest


@pytest.mark.vdsm
def test_always_fails() -> None:
    pytest.fail("Deliberate failure: checking the CI Failed label automation (do not merge)")
