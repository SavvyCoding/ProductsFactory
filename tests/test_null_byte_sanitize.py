"""Null-byte / control-char sanitization on the prompt-build path (2026-08-01).

A single NUL written into session_summary.md by a buggy agent made it into
{prev_session_summary} → the docker-run arg list → `ValueError: embedded null
byte`, crashing every session on SupplyChainOptimizerApp (#36) and wedging 38
features into a designer_bounce loop. These guard the sanitizers that fix it.
"""
import os

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.docker_runner import (
    _strip_control_bytes,
    _sanitize_cmd_args,
    _read_session_summary,
)


class TestStripControlBytes:
    def test_removes_nul(self):
        assert _strip_control_bytes("89.50\x00overage") == "89.50overage"

    def test_removes_c0_but_keeps_tab_newline_cr(self):
        s = "a\tb\nc\rd\x01\x1fe"
        assert _strip_control_bytes(s) == "a\tb\nc\rde"

    def test_clean_string_unchanged(self):
        s = "normal text with unicode ≥70% and\nnewlines"
        assert _strip_control_bytes(s) is s  # fast-path returns the same object

    def test_empty(self):
        assert _strip_control_bytes("") == ""


class TestSanitizeCmdArgs:
    def test_strips_nul_from_any_arg(self):
        cmd = ["docker", "run", "-p", "prompt with \x00 null", "image"]
        out = _sanitize_cmd_args(cmd)
        assert out == ["docker", "run", "-p", "prompt with  null", "image"]
        # every arg is now NUL-free (would no longer crash subprocess)
        assert all("\x00" not in a for a in out if isinstance(a, str))

    def test_clean_cmd_unchanged(self):
        cmd = ["docker", "run", "--rm", "image"]
        assert _sanitize_cmd_args(cmd) == cmd

    def test_non_string_args_pass_through(self):
        cmd = ["docker", 5, None, "clean"]
        assert _sanitize_cmd_args(cmd) == ["docker", 5, None, "clean"]


class TestReadSessionSummarySanitizes:
    def test_nul_in_summary_file_is_stripped(self, tmp_path):
        (tmp_path / "session_summary.md").write_bytes(
            b"progress notes\n- coverage: 89.50\x00overage (>=70)\n")
        out = _read_session_summary(str(tmp_path))
        assert "\x00" not in out
        assert "89.50overage" in out

    def test_missing_file_returns_empty(self, tmp_path):
        assert _read_session_summary(str(tmp_path)) == ""
