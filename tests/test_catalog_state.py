import importlib.util
import json
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / ".github/scripts/catalog_state.py"
WORKFLOW_PATH = ROOT / ".github/workflows/build-app-packages.yml"
SPEC = importlib.util.spec_from_file_location("catalog_state", SCRIPT_PATH)
catalog_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(catalog_state)


class CatalogRepository:
    def __init__(self, root):
        self.root = Path(root)

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args],
            cwd=self.root,
            text=True,
            encoding="utf-8",
        ).strip()

    def initialize(self):
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Catalog Test")
        (self.root / "Apps").mkdir()
        (self.root / "Apps/example.json").write_text(
            json.dumps({"name": "Example", "version": "1.0.0"}) + "\n",
            encoding="utf-8",
        )
        (self.root / "supported_apps.json").write_text(
            json.dumps(
                {
                    "example": (
                        "https://raw.githubusercontent.com/owner/repo/"
                        "main/Apps/example.json"
                    )
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.git("add", ".")
        self.git("commit", "-qm", "Catalog snapshot")
        return self.git("rev-parse", "HEAD")


class CatalogStateTests(unittest.TestCase):
    def setUp(self):
        self.previous_cwd = Path.cwd()
        self.temp_directory = tempfile.TemporaryDirectory()
        self.repo = CatalogRepository(self.temp_directory.name)
        self.catalog_commit = self.repo.initialize()

    def tearDown(self):
        import os

        os.chdir(self.previous_cwd)
        self.temp_directory.cleanup()

    def args(self, output):
        return Namespace(
            catalog_commit=self.catalog_commit,
            package_storage_base_url="https://packages.example.test/catalog/",
            published_at="2026-08-19T20:30:39Z",
            repository="RobinMJD/IntuneBrew",
            run_id=32297922865,
            workflow_name="Build App Packages and Collect App Information",
            workflow_path=".github/workflows/build-app-packages.yml",
            output=str(output),
        )

    def test_generation_is_deterministic_and_uses_numeric_run_id(self):
        import os

        os.chdir(self.repo.root)
        first = self.repo.root / "first.json"
        second = self.repo.root / "second.json"
        catalog_state.generate(self.args(first))
        catalog_state.generate(self.args(second))

        self.assertEqual(first.read_bytes(), second.read_bytes())
        state = json.loads(first.read_text(encoding="utf-8"))
        self.assertEqual(set(state), catalog_state.STATE_KEYS)
        self.assertEqual(state["schemaVersion"], 1)
        self.assertEqual(state["runId"], 32297922865)
        self.assertEqual(state["catalogCommit"], self.catalog_commit)
        self.assertEqual(state["repository"], "RobinMJD/IntuneBrew")
        self.assertEqual(state["publishedAt"], "2026-08-19T20:30:39Z")
        self.assertEqual(
            state["workflowName"],
            "Build App Packages and Collect App Information",
        )
        self.assertEqual(
            state["workflowPath"],
            ".github/workflows/build-app-packages.yml",
        )
        self.assertEqual(
            state["packageStorageBaseUrl"],
            "https://packages.example.test/catalog",
        )

    def test_marker_commit_records_its_catalog_parent_not_itself(self):
        import os

        os.chdir(self.repo.root)
        state_path = self.repo.root / ".github/catalog-state.json"
        catalog_state.generate(self.args(state_path))
        self.repo.git("add", ".github/catalog-state.json")
        self.repo.git("commit", "-qm", "Publish catalog state")

        marker_commit = self.repo.git("rev-parse", "HEAD")
        marker_parent = self.repo.git("rev-parse", "HEAD^")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertNotEqual(state["catalogCommit"], marker_commit)
        self.assertEqual(state["catalogCommit"], marker_parent)

    def test_validation_rejects_a_missing_referenced_app(self):
        import os

        os.chdir(self.repo.root)
        (self.repo.root / "Apps/example.json").unlink()
        self.repo.git("add", "-u")
        self.repo.git("commit", "-qm", "Remove referenced app")
        missing_app_commit = self.repo.git("rev-parse", "HEAD")

        with self.assertRaisesRegex(ValueError, "catalog snapshot|referenced file"):
            catalog_state.validate_catalog_snapshot(missing_app_commit)


class CatalogWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_marker_is_last_and_follows_publication_steps(self):
        process = self.workflow.index("- name: Process apps")
        commit = self.workflow.index("- name: Commit and push changes")
        report = self.workflow.index("- name: Report packaging failures")
        marker = self.workflow.index("- name: Publish catalog state")

        self.assertLess(process, commit)
        self.assertLess(commit, report)
        self.assertLess(report, marker)
        self.assertNotIn("- name:", self.workflow[marker + 1 :])
        self.assertNotIn("pending_requests.py resolve", self.workflow)
        self.assertNotIn("Commit resolved requests", self.workflow)

    def test_marker_requires_proven_success_and_exact_catalog_commit(self):
        marker = self.workflow.split("- name: Publish catalog state", 1)[1]
        self.assertIn(
            "if: success() && steps.process-apps.outputs.packaging_succeeded == 'true'",
            marker,
        )
        self.assertIn(
            "PUBLISHED_CATALOG_COMMIT: "
            "${{ steps.catalog-snapshot.outputs.catalog_commit }}",
            marker,
        )
        self.assertIn('CATALOG_COMMIT="$local_head"', marker)
        self.assertIn('marker_parent=$(git rev-parse HEAD^)', marker)
        self.assertIn('if [ "$marker_parent" != "$local_head" ]; then', marker)
        self.assertIn('if [ "$recorded_commit" != "$marker_parent" ]; then', marker)

    def test_marker_push_is_fail_closed_and_never_forced(self):
        marker = self.workflow.split("- name: Publish catalog state", 1)[1]
        self.assertIn('if [ "$local_head" != "$remote_head" ]; then', marker)
        self.assertIn('if [ "$existing_run_id" -ge "$RUN_ID" ]; then', marker)
        self.assertIn("git push origin HEAD:main", marker)
        self.assertNotIn("git pull --rebase", self.workflow)
        self.assertNotIn("git rebase", self.workflow)
        self.assertNotIn("--force", self.workflow)
        self.assertNotIn("git push -f", self.workflow)

    def test_marker_path_cannot_recurse_into_catalog_workflow(self):
        trigger = self.workflow.split("  schedule:", 1)[0]
        self.assertIn("'.github/scripts/collect_app_info.py'", trigger)
        self.assertNotIn(".github/catalog-state.json", trigger)

    def test_publication_rejects_non_main_dispatch_refs(self):
        collect_job = self.workflow.split("  collect:", 1)[1].split("  build:", 1)[0]
        guard = collect_job.index("- name: Require main branch")
        checkout = collect_job.index("- name: Checkout repository")
        self.assertLess(guard, checkout)
        self.assertIn("if: github.ref != 'refs/heads/main'", collect_job)
        self.assertIn("exit 1", collect_job[guard:checkout])


if __name__ == "__main__":
    unittest.main()
