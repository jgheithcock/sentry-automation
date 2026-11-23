"""Tests for the diagnostic helpers (no network)."""
import os
import subprocess
import tempfile
import unittest

from autofix import doctor
from autofix.github_client import use_gh_for_git_auth


class TokenKindTests(unittest.TestCase):
    def test_kinds(self):
        self.assertIn("fine-grained", doctor.token_kind("github_pat_abc"))
        self.assertIn("gh auth login", doctor.token_kind("gho_abc"))
        self.assertIn("classic", doctor.token_kind("ghp_abc"))
        self.assertEqual(doctor.token_kind("xyz"), "unrecognized token format")


class RepoAccessTests(unittest.TestCase):
    def test_ok(self):
        status, headers = doctor.parse_http_response("HTTP/2.0 200 OK\nContent-Type: json\n\n{}")
        self.assertEqual(status, 200)
        self.assertIsNone(doctor.repo_access_problem(status, headers))

    def test_404_explains_token_owner(self):
        status, headers = doctor.parse_http_response("HTTP/2.0 404 Not Found\n\n{}")
        self.assertIn("Resource owner", doctor.repo_access_problem(status, headers))

    def test_sso_header_wins(self):
        raw = "HTTP/2.0 403 Forbidden\nX-GitHub-SSO: required; url=https://github.com/x\n\n{}"
        status, headers = doctor.parse_http_response(raw)
        self.assertIn("SAML SSO", doctor.repo_access_problem(status, headers))


class ClaudeTests(unittest.TestCase):
    def test_key_formats(self):
        self.assertIsNone(doctor.claude_key_problem("sk-ant-api03-abc"))
        self.assertIn("OpenAI", doctor.claude_key_problem("sk-proj-abc"))
        self.assertIn("OpenAI", doctor.claude_key_problem("sk-abc"))
        self.assertIn("unrecognized", doctor.claude_key_problem("abc"))

    def test_parse_result_json_with_stderr_noise(self):
        out = 'warning: something\n{"is_error":true,"api_error_status":401,"result":"Invalid API key"}'
        data = doctor.parse_claude_json(out)
        self.assertEqual(data["api_error_status"], 401)
        self.assertEqual(doctor.parse_claude_json("not json"), {})

    def test_valid_format_key_is_checked_against_api(self):
        saved = (doctor.anthropic_key_status, os.environ.get("ANTHROPIC_API_KEY"),
                 os.environ.get("CLAUDE_CODE_USE_BEDROCK"))
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-revoked"
        os.environ.pop("CLAUDE_CODE_USE_BEDROCK", None)
        try:
            for status, failed in ((401, 1), (200, 0), (0, 0)):
                doctor.anthropic_key_status = lambda key, s=status: (s, "invalid x-api-key")
                r = doctor.Report()
                doctor.check_claude_credentials(r)
                self.assertEqual(r.failed, failed, status)
        finally:
            doctor.anthropic_key_status = saved[0]
            for k, v in zip(("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK"), saved[1:]):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_hints(self):
        self.assertIn("console.anthropic.com", doctor.claude_error_hint(401, "Invalid API key", False))
        self.assertIn("IAM", doctor.claude_error_hint(401, "", True))
        self.assertIn("Billing", doctor.claude_error_hint(400, "Your credit balance is too low", False))
        self.assertIn("Bedrock console", doctor.claude_error_hint(403, "", True))
        self.assertEqual(doctor.claude_error_hint(None, "weird", False), "")


class GitAuthTests(unittest.TestCase):
    def test_clone_uses_gh_credential_helper_only(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            use_gh_for_git_auth(d)
            out = subprocess.run(
                ["git", "-C", d, "config", "--local", "--get-all",
                 "credential.https://github.com.helper"],
                capture_output=True, text=True, check=True).stdout.splitlines()
            self.assertEqual(out, ["", "!gh auth git-credential"])


if __name__ == "__main__":
    unittest.main()
