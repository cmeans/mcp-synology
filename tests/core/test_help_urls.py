"""Validate that every help_url points to a real section of error-codes.md.

The central HELP_URLS registry in ``core.errors`` must stay in lockstep with
``docs/error-codes.md``. If a section is renamed or removed without updating
the registry (or vice versa), users following a help_url from an error
envelope will land on a dead link.

These tests enforce both directions of that mapping:

1. Every registered help URL must resolve to a real ``## anchor`` in the doc.
2. Every ``## anchor`` in the doc must correspond to a registered code — this
   catches orphaned sections that nothing links to.
3. The anchor portion of each URL must literally equal its error code, so a
   grep for the code immediately finds the right section.
4. Every SynologyError subclass's ``error_code`` must either be in HELP_URLS
   or be explicitly exempt (e.g. ``session_expired``, which is auto-retried
   and never surfaced to users).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mcp_synology.core.errors import (
    GITHUB_DOCS_BASE,
    HELP_URLS,
    ErrorCode,
    SynologyError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_PATH = REPO_ROOT / "docs" / "error-codes.md"

# Error codes that intentionally have no help_url entry. Keep this list
# tight — every addition needs a justification, because "no help" is a
# user-facing regression unless there is a real reason the code cannot
# be surfaced.
EXEMPT_CODES: frozenset[ErrorCode] = frozenset(
    {
        # Auto-retried in the core client. A surfaced session_expired would
        # mean the retry itself failed, which is reported under the
        # underlying failure's code, not under session_expired.
        ErrorCode.SESSION_EXPIRED,
    }
)


def _extract_h2_anchors(text: str) -> set[str]:
    """Return the set of ``## heading`` anchors from a markdown document.

    Only H2 headings are treated as error-code anchors. H1 is reserved for
    the page title and H3 for sub-sections within a code (if we ever add them),
    so a strict H2 rule keeps the anchor namespace unambiguous.
    """
    return set(re.findall(r"^## (\S+)\s*$", text, re.MULTILINE))


def _all_synology_error_subclasses() -> list[type[SynologyError]]:
    """Recursively enumerate every SynologyError subclass."""
    seen: list[type[SynologyError]] = []

    def walk(cls: type[SynologyError]) -> None:
        for sub in cls.__subclasses__():
            seen.append(sub)
            walk(sub)

    walk(SynologyError)
    return seen


@pytest.fixture(scope="module")
def anchors() -> set[str]:
    assert DOCS_PATH.is_file(), f"Troubleshooting doc missing at {DOCS_PATH}"
    return _extract_h2_anchors(DOCS_PATH.read_text())


class TestHelpUrlsResolveToRealAnchors:
    def test_docs_file_exists(self) -> None:
        assert DOCS_PATH.is_file(), f"{DOCS_PATH} not found"

    def test_every_registered_code_has_matching_anchor(self, anchors: set[str]) -> None:
        missing = sorted(code for code in HELP_URLS if code not in anchors)
        assert not missing, (
            f"Codes in HELP_URLS have no matching `## <code>` heading in "
            f"{DOCS_PATH.name}: {missing}. Either add the section or remove "
            f"the HELP_URLS entry."
        )

    def test_every_url_points_at_troubleshooting_doc(self) -> None:
        wrong = sorted(
            (code, url)
            for code, url in HELP_URLS.items()
            if not url.startswith(GITHUB_DOCS_BASE + "#")
        )
        assert not wrong, (
            f"HELP_URLS entries must point at {GITHUB_DOCS_BASE}#<anchor>. Wrong: {wrong}"
        )

    def test_url_anchor_equals_error_code(self) -> None:
        mismatched: list[tuple[str, str]] = []
        for code, url in HELP_URLS.items():
            anchor = url.rsplit("#", 1)[-1]
            if anchor != code:
                mismatched.append((code, anchor))
        assert not mismatched, (
            f"Anchor must literally equal the error code for grep-ability: {mismatched}"
        )

    def test_no_orphan_sections(self, anchors: set[str]) -> None:
        orphans = sorted(anchors - set(HELP_URLS.keys()))
        assert not orphans, (
            f"{DOCS_PATH.name} has `## <anchor>` headings with no matching "
            f"HELP_URLS entry: {orphans}. Either register the code or remove "
            f"the section."
        )


class TestSynologyErrorSubclassCoverage:
    def test_every_subclass_error_code_is_registered_or_exempt(self) -> None:
        uncovered: list[tuple[str, ErrorCode]] = []
        for cls in _all_synology_error_subclasses():
            code = cls.error_code
            if code in HELP_URLS or code in EXEMPT_CODES:
                continue
            uncovered.append((cls.__name__, code))
        assert not uncovered, (
            f"SynologyError subclasses have an error_code with no HELP_URLS "
            f"entry and no exemption: {uncovered}. Add the code to HELP_URLS "
            f"(and a section to error-codes.md) or justify it in "
            f"EXEMPT_CODES in this test file."
        )

    def test_base_error_code_is_registered(self) -> None:
        # SynologyError itself uses dsm_error — the catch-all code. That
        # must always be registered, since any unhandled path ends up there.
        assert SynologyError.error_code == ErrorCode.DSM_ERROR
        assert ErrorCode.DSM_ERROR in HELP_URLS


class TestErrorCodeEnumCoverage:
    """Every ErrorCode member is either documented or explicitly exempt."""

    def test_every_enum_member_is_registered_or_exempt(self) -> None:
        uncovered = [
            code for code in ErrorCode if code not in HELP_URLS and code not in EXEMPT_CODES
        ]
        assert not uncovered, (
            f"ErrorCode members have no HELP_URLS entry and no exemption: "
            f"{[c.value for c in uncovered]}. Add a section to error-codes.md "
            f"or justify the omission in EXEMPT_CODES."
        )

    def test_help_urls_keys_are_all_valid_error_codes(self) -> None:
        # Catch accidental string drift: every HELP_URLS key must be an
        # actual ErrorCode value (not a typo or renamed code).
        valid_values = {c.value for c in ErrorCode}
        invalid = sorted(set(HELP_URLS.keys()) - valid_values)
        assert not invalid, (
            f"HELP_URLS has keys that are not ErrorCode values: {invalid}. "
            f"Add them to ErrorCode or remove from HELP_URLS."
        )
