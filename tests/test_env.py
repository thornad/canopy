"""Tests for canopy.env utilities."""

import os
from unittest.mock import patch

import pytest

from canopy.env import _dedupe_path, build_mcp_env, build_mcp_path, resolve_command


class TestDedupePath:
    def test_removes_duplicates(self):
        assert _dedupe_path("/usr/bin:/usr/bin:/bin") == "/usr/bin:/bin"

    def test_removes_empty(self):
        assert _dedupe_path("/usr/bin::/bin:") == "/usr/bin:/bin"

    def test_preserves_order(self):
        assert _dedupe_path("/a:/b:/a:/c") == "/a:/b:/c"


class TestBuildMcpPath:
    def test_uses_existing_path(self):
        env = {"PATH": "/usr/bin:/bin"}
        result = build_mcp_path(env)
        assert "/usr/bin" in result
        assert "/bin" in result

    def test_prepends_shell_path_when_stripped(self):
        # Simulate launchd default
        stripped = "/usr/bin:/bin:/usr/sbin:/sbin"
        with patch("canopy.env.get_shell_path", return_value="/opt/homebrew/bin:/usr/local/bin"):
            result = build_mcp_path({"PATH": stripped})
        assert result.startswith("/opt/homebrew/bin:/usr/local/bin")
        assert "/usr/bin" in result
        assert "/bin" in result

    def test_does_not_duplicate(self):
        stripped = "/usr/bin:/bin:/usr/sbin:/sbin"
        with patch("canopy.env.get_shell_path", return_value="/usr/bin:/bin"):
            result = build_mcp_path({"PATH": stripped})
        parts = result.split(":")
        assert parts.count("/usr/bin") == 1
        assert parts.count("/bin") == 1

    def test_custom_extra_paths(self):
        env = {"PATH": "/usr/bin"}
        result = build_mcp_path(env, extra_paths=["/my/custom/bin"])
        assert "/my/custom/bin" in result


class TestResolveCommand:
    def test_returns_absolute_unchanged(self):
        assert resolve_command("/usr/bin/python3", "/usr/bin") == "/usr/bin/python3"

    def test_resolves_in_path(self, tmp_path):
        fake_bin = tmp_path / "my-cmd"
        fake_bin.write_text("#!/bin/bash\necho hi")
        fake_bin.chmod(0o755)
        path = str(tmp_path) + ":/usr/bin"
        assert resolve_command("my-cmd", path) == str(fake_bin)

    def test_returns_original_when_not_found(self):
        assert resolve_command("nonexistent-cmd", "/usr/bin") == "nonexistent-cmd"

    def test_handles_empty(self):
        assert resolve_command("", "/usr/bin") == ""


class TestBuildMcpEnv:
    def test_applies_overrides(self):
        env = build_mcp_env(
            base_env={"PATH": "/usr/bin", "HOME": "/Users/test"},
            overrides={"API_KEY": "secret"},
        )
        assert env["API_KEY"] == "secret"
        assert "/usr/bin" in env["PATH"]

    def test_dedupes_overridden_path(self):
        env = build_mcp_env(
            base_env={"PATH": "/usr/bin:/bin"},
            overrides={"PATH": "/usr/bin:/usr/bin:/custom"},
        )
        parts = env["PATH"].split(":")
        assert parts.count("/usr/bin") == 1
        assert "/custom" in parts

    def test_no_base_env_uses_os_environ(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin", "MYVAR": "x"}, clear=False):
            env = build_mcp_env()
            assert "/usr/bin" in env["PATH"]
            assert env["MYVAR"] == "x"

    def test_strips_bundled_python_vars(self):
        env = build_mcp_env(
            base_env={
                "PATH": "/usr/bin",
                "PYTHONHOME": "/App.app/Contents/Frameworks/cpython-3.11",
                "PYTHONPATH": "/App.app/Contents/Resources",
                "PYTHONDONTWRITEBYTECODE": "1",
                "HOME": "/Users/test",
            },
        )
        assert "PYTHONHOME" not in env
        assert "PYTHONPATH" not in env
        assert "PYTHONDONTWRITEBYTECODE" not in env
        assert env["HOME"] == "/Users/test"

    def test_override_can_set_pythonpath(self):
        env = build_mcp_env(
            base_env={"PATH": "/usr/bin", "PYTHONHOME": "/bundled"},
            overrides={"PYTHONPATH": "/custom/lib"},
        )
        assert "PYTHONHOME" not in env
        assert env["PYTHONPATH"] == "/custom/lib"
