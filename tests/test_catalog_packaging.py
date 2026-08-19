import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github/workflows/build-app-packages.yml"
GENERATOR_PATH = ROOT / ".github/scripts/generate_supported_apps.py"

SPEC = importlib.util.spec_from_file_location("generate_supported_apps", GENERATOR_PATH)
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)


class CatalogPublicationContractTests(unittest.TestCase):
    def test_prepackaging_catalog_keeps_every_non_deprecated_manifest(self):
        supported = json.loads(
            (ROOT / "supported_apps.json").read_text(encoding="utf-8")
        )
        expected = {
            path.stem
            for path in (ROOT / "Apps").glob("*.json")
            if not json.loads(path.read_text(encoding="utf-8")).get("deprecated")
        }
        self.assertEqual(set(supported), expected)

    def test_percent_encoded_deployable_filename_is_accepted(self):
        app = {
            "name": "Example",
            "version": "1",
            "bundleId": "com.example.app",
            "url": "https://example.test/Example%20App.dmg",
            "fileName": "Example%20App.dmg",
            "sha": "a" * 64,
        }
        self.assertEqual(generator.publication_errors(app), [])

    def test_unsafe_or_archive_filename_is_rejected(self):
        app = {
            "name": "Example",
            "version": "1",
            "bundleId": "com.example.app",
            "url": "https://example.test/example.zip",
            "fileName": "C/C++/example.zip",
            "sha": "a" * 64,
        }
        errors = generator.publication_errors(app)
        self.assertIn("unsafe filename", errors)
        self.assertIn("non-deployable filename", errors)

    def test_legacy_blob_package_is_rejected(self):
        app = {
            "name": "Example",
            "version": "1",
            "bundleId": "com.example.app",
            "url": "https://intunebrew.blob.core.windows.net/pkg/example.pkg",
            "fileName": "example.pkg",
            "sha": "a" * 64,
        }
        self.assertIn("legacy package URL", generator.publication_errors(app))

    def test_strict_generator_fails_without_rewriting_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            apps = root / "Apps"
            apps.mkdir()
            (apps / "good.json").write_text(
                json.dumps(
                    {
                        "name": "Good",
                        "version": "1",
                        "bundleId": "com.example.good",
                        "url": "https://example.test/good.dmg",
                        "fileName": "good.dmg",
                        "sha": "a" * 64,
                    }
                ),
                encoding="utf-8",
            )
            (apps / "bad.json").write_text(
                json.dumps({"name": "Bad", "fileName": "bad.zip"}),
                encoding="utf-8",
            )
            (root / "README.md").write_text(
                "Apps_Available-0-2ea44f?style=flat", encoding="utf-8"
            )
            (root / "supported_apps.json").write_text(
                '{"existing": "unchanged"}\n', encoding="utf-8"
            )
            with patch.object(generator, "ROOT", root), patch.object(
                generator, "APPS_DIR", apps
            ), patch.object(
                generator, "SUPPORTED_PATH", root / "supported_apps.json"
            ), patch.object(generator, "README_PATH", root / "README.md"):
                with self.assertRaises(SystemExit) as context:
                    generator.generate_supported_apps()

            self.assertIn("bad.json", str(context.exception))
            self.assertEqual(
                (root / "supported_apps.json").read_text(encoding="utf-8"),
                '{"existing": "unchanged"}\n',
            )

    def test_prepackaging_generator_keeps_invalid_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            apps = root / "Apps"
            apps.mkdir()
            (apps / "candidate.json").write_text(
                json.dumps({"name": "Candidate", "fileName": "candidate.zip"}),
                encoding="utf-8",
            )
            (root / "README.md").write_text(
                "Apps_Available-0-2ea44f?style=flat", encoding="utf-8"
            )
            with patch.object(generator, "APPS_DIR", apps), patch.object(
                generator, "SUPPORTED_PATH", root / "supported_apps.json"
            ), patch.object(generator, "README_PATH", root / "README.md"):
                generator.generate_supported_apps(allow_incomplete=True)

            supported = json.loads(
                (root / "supported_apps.json").read_text(encoding="utf-8")
            )
            self.assertEqual(set(supported), {"candidate"})


class WorkflowPackagingRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.process = cls.workflow.split("- name: Process apps", 1)[1]

    def test_direct_xar_pkg_routes_as_pkg_without_archive_extraction(self):
        pkg_route = self.process.split("case \"$kind\" in", 1)[1].split("dmg)", 1)[0]
        self.assertIn("pkg)", pkg_route)
        self.assertIn("direct PKG", pkg_route)
        self.assertNotIn("ditto -x -k", pkg_route)
        self.assertNotIn("unzip", pkg_route)

    def test_compressed_dmg_routes_by_url_and_mounts(self):
        dmg_route = self.process.split("dmg)", 1)[1].split("archive)", 1)[0]
        self.assertIn("hdiutil attach", dmg_route)
        self.assertNotIn("file -b", self.process)

    def test_dmg_falls_back_to_declared_top_level_app(self):
        self.assertIn('find_app_payload "$mount_dir" "$declared_app"', self.process)
        self.assertIn('-name "*.app"', self.process)
        self.assertIn("-prune -print", self.process)

    def test_nested_dmg_is_mounted_only_after_archive_extraction(self):
        archive_route = self.process.split("archive)", 1)[1].split("*)", 1)[0]
        self.assertIn("ditto -x -k", archive_route)
        self.assertIn('nested_dmg=$(find "$extract_dir"', archive_route)
        self.assertIn("hdiutil attach \"$nested_dmg\"", archive_route)

    def test_tgz_and_persisted_extensionless_archive_formats_are_supported(self):
        self.assertIn("*.tar.gz|*.tgz", self.process)
        self.assertIn(".artifact_kind // empty", self.process)
        self.assertIn(".archive_format // empty", self.process)
        self.assertIn('case "$archive_format" in', self.process)

    def test_archive_without_app_or_pkg_is_a_packaging_failure(self):
        self.assertIn("No .app or .pkg found inside archive", self.process)
        self.assertIn('FAILED_APPS+=("$app_name")', self.process)

    def test_exact_private_blob_reuse_precedes_any_download(self):
        reuse = self.process.index('if [ "$reuse_existing_blob" = true ]; then')
        download = self.process.index("Downloading $app_name...")
        self.assertLess(reuse, download)
        self.assertIn('.vendor_url // empty', self.process[:download])
        self.assertIn("is_storage_package_url", self.process[:download])

    def test_failed_manifests_are_restored_and_counted_once(self):
        loop_end = self.process.index("# Display summary")
        before_summary = self.process[:loop_end]
        self.assertIn('checkout HEAD -- "Apps/${failed_app}.json"', before_summary)
        self.assertIn(
            'total_apps_processed=$((${#SUCCESSFUL_APPS[@]} + ${#FAILED_APPS[@]}))',
            self.process,
        )
        self.assertIn('failed_count=${#FAILED_APPS[@]}', self.process)

    def test_blob_deletion_is_deferred_until_after_marker_publication(self):
        marker = self.workflow.index("- name: Publish catalog state")
        refresh_login = self.workflow.index(
            "- name: Refresh Azure login for package cleanup"
        )
        cleanup = self.workflow.index("- name: Delete superseded package blobs")
        self.assertNotIn("az storage blob delete", self.workflow[:marker])
        self.assertLess(marker, refresh_login)
        self.assertLess(refresh_login, cleanup)
        self.assertIn("uses: azure/login@v3", self.workflow[refresh_login:cleanup])
        self.assertLess(marker, cleanup)
        self.assertIn("git push origin HEAD:main", self.workflow[marker:cleanup])

    def test_failed_or_unpublished_run_cannot_delete_old_blobs(self):
        require = self.workflow.index("- name: Require successful packaging")
        marker = self.workflow.index("- name: Publish catalog state")
        cleanup = self.workflow.index("- name: Delete superseded package blobs")
        self.assertLess(require, marker)
        self.assertLess(marker, cleanup)
        cleanup_step = self.workflow[cleanup:]
        self.assertIn("if ! az storage blob delete", cleanup_step)
        self.assertIn("::warning::Could not delete superseded blob", cleanup_step)

    def test_strict_index_generation_runs_after_packaging(self):
        pre = self.workflow.index(
            "python .github/scripts/generate_supported_apps.py --allow-incomplete"
        )
        process = self.workflow.index("- name: Process apps")
        final = self.workflow.index(
            "run: python .github/scripts/generate_supported_apps.py", process
        )
        marker = self.workflow.index("- name: Publish catalog state")
        self.assertLess(pre, process)
        self.assertLess(process, final)
        self.assertLess(final, marker)


if __name__ == "__main__":
    unittest.main()
