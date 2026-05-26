"""GitHub release selection helpers.

Kept separate from app.py so the logic is unit-testable without PyObjC.
"""

from typing import Any

from packaging.version import InvalidVersion, Version


def select_latest_stable_release(
    releases: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Pick the highest stable release from a GitHub /releases response.

    Filters out drafts, prereleases (both GitHub flag and PEP 440), and
    unparseable tags so dev/rc publishes never surface as "latest".
    """
    best_release: dict[str, Any] | None = None
    best_version: Version | None = None

    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = release.get("tag_name")
        if not tag:
            continue
        try:
            version = Version(tag.lstrip("v"))
        except InvalidVersion:
            continue
        if version.is_prerelease:
            continue
        if best_version is None or version > best_version:
            best_version = version
            best_release = release

    return best_release
