"""Run the standard integration tests against virtual-dsm.

This module re-exports test classes from test_integration.py so they run
against the virtual-dsm container instead of a real NAS. The nas_client
fixture in this directory's conftest.py provides the virtual-dsm connection.

Run with: uv run pytest -m vdsm -v --log-cli-level=INFO
Override version: uv run pytest -m vdsm --dsm-version 7.1
"""

from __future__ import annotations

import pytest

from tests.test_integration import (
    TestConnection,
    TestErrorHandling,
    TestFileTransfers,
    TestListing,
    TestMetadata,
    TestRecycleBin,
    TestResourceUsage,
    TestSearch,
    TestSystemInfo,
    TestWriteOperations,
)

pytestmark = pytest.mark.vdsm

# Re-export all test classes. Pytest collects them here and uses
# the nas_client fixture from tests/vdsm/conftest.py (which points
# at the virtual-dsm container) instead of the one in test_integration.py
# (which loads from integration_config.yaml).

__all__ = [
    "TestConnection",
    "TestErrorHandling",
    "TestFileTransfers",
    "TestListing",
    "TestMetadata",
    "TestRecycleBin",
    "TestResourceUsage",
    "TestSearch",
    "TestSystemInfo",
    "TestWriteOperations",
]
