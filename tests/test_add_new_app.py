import importlib.util
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / ".github" / "scripts" / "add_new_app.py"
SPEC = importlib.util.spec_from_file_location("add_new_app", SCRIPT_PATH)
add_new_app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(add_new_app)


def cask_data(url, artifacts):
    return {"url": url, "artifacts": artifacts}


class CaskArtifactValidationTests(unittest.TestCase):
    def test_codex_cli_is_rejected(self):
        codex = cask_data(
            "https://example.test/codex-package-aarch64-apple-darwin.tar.gz",
            [{"binary": ["bin/codex"], "target": "/opt/homebrew/bin/codex"}],
        )

        self.assertIn("command-line binaries", add_new_app.binary_only_cask_reason(codex))

    def test_request_processor_does_not_write_rejected_codex_cli(self):
        codex = cask_data(
            "https://example.test/codex-package-aarch64-apple-darwin.tar.gz",
            [{"binary": ["bin/codex"], "target": "/opt/homebrew/bin/codex"}],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            script_path = root / ".github" / "scripts" / "collect_app_info.py"
            script_path.parent.mkdir(parents=True)
            original = "app_urls = []\n"
            script_path.write_text(original, encoding="utf-8")

            previous_cwd = os.getcwd()
            try:
                os.chdir(root)
                with (
                    patch.dict(
                        os.environ,
                        {
                            "ISSUE_TITLE": "Add Codex",
                            "ISSUE_BODY": "",
                            "COMMENT_BODY": "/approve codex",
                        },
                        clear=True,
                    ),
                    patch.object(
                        add_new_app,
                        "fetch_homebrew_info",
                        return_value=codex,
                    ),
                    self.assertRaises(SystemExit) as context,
                ):
                    add_new_app.main()
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(context.exception.code, 1)
            self.assertEqual(script_path.read_text(encoding="utf-8"), original)

    def test_codex_desktop_app_is_accepted(self):
        codex_app = cask_data(
            "https://example.test/Codex-darwin-arm64.zip",
            [{"app": ["Codex.app"], "target": "/Applications/Codex.app"}],
        )

        self.assertIsNone(add_new_app.binary_only_cask_reason(codex_app))

    def test_binary_only_cli_archive_is_rejected(self):
        cli = cask_data(
            "https://example.test/tool-darwin-arm64.tar.gz",
            [{"binary": ["tool"], "target": "/opt/homebrew/bin/tool"}],
        )

        self.assertIsNotNone(add_new_app.binary_only_cask_reason(cli))

    def test_cli_installer_script_does_not_count_as_an_app_payload(self):
        cli = cask_data(
            "https://example.test/cli-sdk.tar.gz",
            [
                {"binary": ["bin/cli"], "target": "/opt/homebrew/bin/cli"},
                {"installer": {"script": "install.sh"}},
            ],
        )

        self.assertIsNotNone(add_new_app.binary_only_cask_reason(cli))

    def test_deployable_app_archive_is_accepted(self):
        desktop_app = cask_data(
            "https://example.test/DesktopApp.zip",
            [{"app": ["Desktop App.app"], "target": "/Applications/Desktop App.app"}],
        )

        self.assertIsNone(add_new_app.binary_only_cask_reason(desktop_app))
        self.assertEqual(
            add_new_app.determine_app_type(desktop_app),
            ("app_urls", "app"),
        )


class ApprovalWorkflowTests(unittest.TestCase):
    def test_approval_workflows_test_catalog_before_committing(self):
        for filename in (
            "auto-approve-app-request.yml",
            "approve-app-request.yml",
        ):
            with self.subTest(workflow=filename):
                workflow = (
                    ROOT / ".github" / "workflows" / filename
                ).read_text(encoding="utf-8")
                add_app = workflow.index("- name: Add app to list")
                run_tests = workflow.index("- name: Run catalog regression tests")
                commit = workflow.index("- name: Commit changes")

                self.assertLess(add_app, run_tests)
                self.assertLess(run_tests, commit)
                self.assertIn(
                    'python -m unittest discover -s tests -p "test_*.py"',
                    workflow[run_tests:commit],
                )
                self.assertNotIn("actions: write", workflow)
                self.assertIn("contents: read", workflow)


if __name__ == "__main__":
    unittest.main()
