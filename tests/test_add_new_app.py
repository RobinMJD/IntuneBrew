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

    def test_binary_with_suite_artifact_is_rejected(self):
        flutter = cask_data(
            "https://example.test/flutter-macos.zip",
            [
                {"suite": ["flutter"]},
                {"binary": ["flutter/bin/flutter"]},
            ],
        )

        self.assertIsNotNone(add_new_app.binary_only_cask_reason(flutter))

    def test_binary_with_prefpane_artifact_is_rejected(self):
        preference_pane = cask_data(
            "https://example.test/SwiftDefaultApps.prefPane.zip",
            [
                {"prefpane": ["SwiftDefaultApps.prefPane"]},
                {"binary": ["swda"]},
            ],
        )

        self.assertIsNotNone(
            add_new_app.binary_only_cask_reason(preference_pane)
        )

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

    def test_archive_with_pkg_artifact_is_accepted(self):
        packaged_app = cask_data(
            "https://example.test/DesktopApp.zip",
            [{"pkg": ["Desktop App.pkg"]}],
        )

        self.assertIsNone(add_new_app.binary_only_cask_reason(packaged_app))

    def test_installer_only_cask_is_rejected_before_list_write(self):
        bootstrap = cask_data(
            "https://example.test/bootstrap.zip",
            [{"installer": [{"manual": "Bootstrap.app"}]}],
        )
        self.assertIn(
            "bootstrap installer",
            add_new_app.unsupported_cask_reason(bootstrap),
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

    def test_catalog_workflow_uses_only_its_job_scoped_token(self):
        workflow = (
            ROOT / ".github" / "workflows" / "build-app-packages.yml"
        ).read_text(encoding="utf-8")

        self.assertNotIn("secrets.PAT", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("issues: write", workflow)
        self.assertIn("id-token: write", workflow)

    def test_catalog_collection_runs_on_hosted_ubuntu(self):
        workflow = (
            ROOT / ".github" / "workflows" / "build-app-packages.yml"
        ).read_text(encoding="utf-8")

        collect = workflow.index("  collect:")
        build = workflow.index("  build:")
        collect_app_info = workflow.index(
            "python .github/scripts/collect_app_info.py"
        )

        self.assertLess(collect, collect_app_info)
        self.assertLess(collect_app_info, build)
        collect_job = workflow[collect:build]
        self.assertIn("runs-on: ubuntu-latest", collect_job)
        self.assertNotIn("self-hosted", collect_job)
        self.assertIn("uses: actions/setup-python@v5", collect_job)
        self.assertIn('python-version: "3.x"', collect_job)
        self.assertIn("contents: read", collect_job)
        self.assertNotIn("contents: write", collect_job)
        self.assertIn("actions/upload-artifact@v4", collect_job)
        self.assertIn("needs: collect", workflow[build:])
        self.assertIn("runs-on: macos-latest", workflow[build:])
        self.assertIn("actions/download-artifact@v4", workflow[build:])
        self.assertIn("needs.collect.outputs.scope", workflow[build:])


class CatalogStorageWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (
            ROOT / ".github" / "workflows" / "build-app-packages.yml"
        ).read_text(encoding="utf-8")

    def test_storage_uses_oidc_environment_variables(self):
        self.assertNotIn("AZURE_STORAGE_CONNECTION_STRING", self.workflow)
        self.assertNotIn("secrets.", self.workflow)

        build = self.workflow.index("  build:")
        permissions = self.workflow.index("    permissions:", build)
        steps = self.workflow.index("    steps:", permissions)
        build_permissions = self.workflow[permissions:steps]
        self.assertIn("contents: write", build_permissions)
        self.assertIn("issues: write", build_permissions)
        self.assertIn("id-token: write", build_permissions)
        self.assertNotIn("actions: write", build_permissions)

        login = self.workflow.index("uses: azure/login@v3", build)
        first_storage_operation = self.workflow.index("az storage blob", build)
        self.assertLess(login, first_storage_operation)
        self.assertIn("AZURE_LOGIN_POST_CLEANUP: true", self.workflow)

        for variable, login_input in (
            ("AZURE_CLIENT_ID", "client-id"),
            ("AZURE_TENANT_ID", "tenant-id"),
            ("AZURE_SUBSCRIPTION_ID", "subscription-id"),
        ):
            self.assertIn(
                f"{login_input}: ${{{{ vars.{variable} }}}}",
                self.workflow,
            )

        for variable in (
            "AZURE_STORAGE_ACCOUNT",
            "AZURE_STORAGE_CONTAINER",
            "AZURE_STORAGE_BASE_URL",
        ):
            self.assertIn(
                f"{variable}: ${{{{ vars.{variable} }}}}",
                self.workflow,
            )

    def test_oidc_job_uses_catalog_storage_environment_subject(self):
        build = self.workflow.index("  build:")
        permissions = self.workflow.index("    permissions:", build)
        build_header = self.workflow[build:permissions]

        self.assertIn("    environment: catalog-storage", build_header)
        self.assertIn("    runs-on: macos-latest", build_header)

        for workflow_path in (ROOT / ".github" / "workflows").glob("*.yml"):
            workflow = workflow_path.read_text(encoding="utf-8")
            if "uses: azure/login@" in workflow or "vars.AZURE_" in workflow:
                with self.subTest(workflow=workflow_path.name):
                    self.assertIn(
                        "environment: catalog-storage",
                        workflow,
                        "Azure OIDC jobs must bind the environment-scoped "
                        "variables and federated subject",
                    )

    def test_storage_configuration_is_validated_before_login(self):
        validation = self.workflow.index(
            "- name: Validate Azure storage configuration"
        )
        login = self.workflow.index("- name: Log in to Azure with GitHub OIDC")
        self.assertLess(validation, login)

        validation_step = self.workflow[validation:login]
        for variable in (
            "AZURE_CLIENT_ID",
            "AZURE_TENANT_ID",
            "AZURE_SUBSCRIPTION_ID",
            "AZURE_STORAGE_ACCOUNT",
            "AZURE_STORAGE_CONTAINER",
            "AZURE_STORAGE_BASE_URL",
        ):
            self.assertIn(variable, validation_step)
        self.assertIn(
            "::error::Missing required catalog-storage environment variables",
            validation_step,
        )

    def test_collector_scrapers_are_hosted_ubuntu_compatible(self):
        scraper_dir = ROOT / ".github" / "scripts" / "scrapers"
        for scraper_path in scraper_dir.glob("*.sh"):
            scraper = scraper_path.read_text(encoding="utf-8")
            with self.subTest(scraper=scraper_path.name):
                self.assertNotIn("sed -i ''", scraper)

    def test_blob_commands_use_configured_login_authentication(self):
        logical_workflow = self.workflow.replace("\\\n", " ")
        commands = [
            line.strip()
            for line in logical_workflow.splitlines()
            if "az storage blob " in line
        ]
        self.assertGreater(len(commands), 0)

        for command in commands:
            with self.subTest(command=command):
                self.assertIn(
                    '--account-name "$AZURE_STORAGE_ACCOUNT"',
                    command,
                )
                self.assertIn(
                    '--container-name "$AZURE_STORAGE_CONTAINER"',
                    command,
                )
                self.assertIn("--auth-mode login", command)

    def test_catalog_package_urls_use_configured_base_url(self):
        self.assertNotIn(
            "intunebrew.blob.core.windows.net/pkg",
            self.workflow,
        )
        self.assertIn(
            'STORAGE_BASE_URL="${AZURE_STORAGE_BASE_URL%/}"',
            self.workflow,
        )
        self.assertIn('"$STORAGE_BASE_URL"/*)', self.workflow)
        self.assertIn(
            'azure_url="${STORAGE_BASE_URL}/${new_blob_name}"',
            self.workflow,
        )


if __name__ == "__main__":
    unittest.main()
