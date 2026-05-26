"""Tests for canopy_app.release_check."""

from canopy.release_check import select_latest_stable_release


def _release(tag, draft=False, prerelease=False):
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease, "html_url": f"https://example.com/{tag}"}


class TestSelectLatestStableRelease:
    def test_picks_highest_stable(self):
        releases = [_release("v0.1.0"), _release("v0.3.0"), _release("v0.2.0")]
        result = select_latest_stable_release(releases)
        assert result["tag_name"] == "v0.3.0"

    def test_skips_drafts(self):
        releases = [_release("v0.5.0", draft=True), _release("v0.2.0")]
        result = select_latest_stable_release(releases)
        assert result["tag_name"] == "v0.2.0"

    def test_skips_github_prerelease_flag(self):
        releases = [_release("v0.5.0", prerelease=True), _release("v0.2.0")]
        result = select_latest_stable_release(releases)
        assert result["tag_name"] == "v0.2.0"

    def test_skips_pep440_prerelease(self):
        releases = [_release("v0.5.0.dev1"), _release("v0.4.0rc1"), _release("v0.3.0")]
        result = select_latest_stable_release(releases)
        assert result["tag_name"] == "v0.3.0"

    def test_returns_none_when_no_stable(self):
        releases = [_release("v0.1.0", draft=True), _release("v0.2.0rc1")]
        assert select_latest_stable_release(releases) is None

    def test_returns_none_for_empty(self):
        assert select_latest_stable_release([]) is None

    def test_skips_unparseable_tags(self):
        releases = [{"tag_name": "nightly-2025", "draft": False, "prerelease": False}, _release("v0.1.0")]
        result = select_latest_stable_release(releases)
        assert result["tag_name"] == "v0.1.0"
