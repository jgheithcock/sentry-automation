"""Tests for the .env-only configuration rules."""
import os
import tempfile
import unittest
from pathlib import Path

from autofix import config

GOOD_ENV = """
GH_TOKEN=gh-from-dotenv
SENTRY_TOKEN=sentry-token
SENTRY_ORG=my-org
SENTRY_API_BASE=https://sentry.io/api/0/
AUTOFIX_PROJECTS=billing=me/billing, web=me/web-app
ANTHROPIC_API_KEY=sk-test
"""


class DotenvTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in config.MANAGED_ENV}
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def env_file(self, text: str) -> Path:
        p = Path(self.tmp.name) / ".env"
        p.write_text(text)
        return p

    def test_load_reads_everything_from_dotenv(self):
        cfg = config.load(env_path=self.env_file(GOOD_ENV))
        self.assertEqual(cfg.sentry_org, "my-org")
        self.assertEqual(cfg.sentry_api_base, "https://sentry.io/api/0")
        self.assertEqual([p.github_repo for p in cfg.projects], ["me/billing", "me/web-app"])

    def test_dotenv_replaces_shell_values(self):
        os.environ["GH_TOKEN"] = "gh-from-shell"
        os.environ["GITHUB_TOKEN"] = "other-shell-token"
        config.apply_dotenv(self.env_file(GOOD_ENV))
        self.assertEqual(os.environ["GH_TOKEN"], "gh-from-dotenv")
        self.assertNotIn("GITHUB_TOKEN", os.environ)

    def test_shell_value_does_not_fill_a_gap(self):
        os.environ["SENTRY_TOKEN"] = "from-shell"
        text = GOOD_ENV.replace("SENTRY_TOKEN=sentry-token", "SENTRY_TOKEN=")
        with self.assertRaises(config.ConfigError) as ctx:
            config.apply_dotenv(self.env_file(text))
        self.assertIn("SENTRY_TOKEN", str(ctx.exception))

    def test_missing_dotenv(self):
        with self.assertRaises(config.ConfigError):
            config.apply_dotenv(Path(self.tmp.name) / "nope.env")

    def test_claude_needs_exactly_one_credential(self):
        both = GOOD_ENV + "CLAUDE_CODE_USE_BEDROCK=1\nAWS_REGION=us-east-1\n"
        neither = GOOD_ENV.replace("ANTHROPIC_API_KEY=sk-test", "")
        for text in (both, neither):
            with self.assertRaises(config.ConfigError):
                config.apply_dotenv(self.env_file(text))

    def test_claude_login_is_a_third_exclusive_option(self):
        login = GOOD_ENV.replace("ANTHROPIC_API_KEY=sk-test", "AUTOFIX_CLAUDE_LOGIN=1")
        config.apply_dotenv(self.env_file(login))
        self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
        with self.assertRaises(config.ConfigError):
            config.apply_dotenv(self.env_file(GOOD_ENV + "AUTOFIX_CLAUDE_LOGIN=1\n"))

    def test_bedrock_needs_region(self):
        text = GOOD_ENV.replace("ANTHROPIC_API_KEY=sk-test", "CLAUDE_CODE_USE_BEDROCK=1")
        with self.assertRaises(config.ConfigError):
            config.apply_dotenv(self.env_file(text))

    def test_project_paths(self):
        a, b = config.parse_projects("a=org/mono:lambdas/a/, b=org/mono")
        self.assertEqual((a.github_repo, a.code_path), ("org/mono", "lambdas/a"))
        self.assertEqual((b.github_repo, b.code_path), ("org/mono", ""))

    def test_bad_project_spec(self):
        for spec in ("billing", "billing=me", "=me/x", "billing=me/x/y",
                     "a=org/mono:../x", "a=/x:y"):
            with self.assertRaises(config.ConfigError):
                config.parse_projects(spec)


class BehaviorSettingsTests(unittest.TestCase):
    def test_behavior_settings_flow_into_config(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text(GOOD_ENV)
            cfg = config.load(env_path=env)
        self.assertEqual(cfg.labels["queued"], config.LABELS["queued"])
        self.assertEqual(cfg.workdir, config.REPO_ROOT / config.WORKDIR)


if __name__ == "__main__":
    unittest.main()
