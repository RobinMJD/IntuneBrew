import importlib.util
import json
import os
import re
import shutil
import subprocess
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
        cls.payload_helpers = cls.process.split(
            "safe_artifact_relative_path() {", 1
        )[1].split("safe_blob_leaf() {", 1)[0]
        cls.payload_helpers = (
            "safe_artifact_relative_path() {" + cls.payload_helpers
        )

    def run_payload_helper(self, root, expected, payload_type="app"):
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        root_arg = str(root)
        if os.name == "nt":
            root_arg = root_arg.replace("\\", "/")
        function_name = (
            "find_app_payload" if payload_type == "app" else "find_pkg_payload"
        )
        script = (
            self.payload_helpers
            + f'\n{function_name} "$1" "$2"\n'
        )
        return subprocess.run(
            [bash, "-c", script, "_", root_arg, expected],
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "MSYS2_ARG_CONV_EXCL": "*"},
        )

    def test_direct_xar_pkg_routes_as_pkg_without_archive_extraction(self):
        pkg_route = self.process.split("case \"$kind\" in", 1)[1].split(
            "dmg|dmg_gzip)", 1
        )[0]
        self.assertIn("pkg)", pkg_route)
        self.assertIn("direct PKG", pkg_route)
        self.assertNotIn("ditto -x -k", pkg_route)
        self.assertNotIn("unzip", pkg_route)

    def test_compressed_dmg_routes_by_url_and_mounts(self):
        dmg_route = self.process.split("dmg)", 1)[1].split("archive)", 1)[0]
        self.assertIn("hdiutil attach", dmg_route)
        self.assertNotIn("file -b", self.process)

    def test_gzip_dmg_is_decompressed_before_mounting(self):
        route = self.process.split("dmg|dmg_gzip)", 1)[1].split("archive)", 1)[0]
        self.assertIn('if [ "$kind" = "dmg_gzip" ]', route)
        self.assertIn('gunzip -c "${download_path}.dmg.gz"', route)
        self.assertIn('hdiutil attach "${download_path}.dmg"', route)
        self.assertLess(route.index("gunzip -c"), route.index("hdiutil attach"))
        self.assertNotIn("payload.pkg", route)

    def test_dmg_falls_back_to_declared_top_level_app(self):
        self.assertIn('find_app_payload "$mount_dir" "$declared_app"', self.process)
        self.assertIn('-name "*.app"', self.process)
        self.assertIn("-prune -print", self.process)

    def test_declared_payload_supports_basename_and_nested_relative_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            basename = root / "Product.app" / "Contents"
            basename.mkdir(parents=True)
            (basename / "Info.plist").write_text("plist", encoding="utf-8")
            result = self.run_payload_helper(root, "Product.app")
            self.assertEqual(result.returncode, 0)
            self.assertTrue(result.stdout.strip().endswith("Product.app"))

            nested = root / "Airfoil" / "Airfoil.app" / "Contents"
            nested.mkdir(parents=True)
            (nested / "Info.plist").write_text("plist", encoding="utf-8")
            result = self.run_payload_helper(root, "Airfoil/Airfoil.app")
            self.assertEqual(result.returncode, 0)
            self.assertTrue(result.stdout.strip().endswith("Airfoil.app"))

            pkg = root / "ELAN" / "Installer.pkg"
            pkg.parent.mkdir(exist_ok=True)
            pkg.write_bytes(b"pkg")
            result = self.run_payload_helper(root, "ELAN/Installer.pkg", "pkg")
            self.assertEqual(result.returncode, 0)
            self.assertTrue(result.stdout.strip().endswith("Installer.pkg"))

    def test_declared_payload_allows_one_deterministic_wrapper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "release-1" / "Airfoil" / "Airfoil.app" / "Contents"
            app.mkdir(parents=True)
            (app / "Info.plist").write_text("plist", encoding="utf-8")

            result = self.run_payload_helper(root, "Airfoil/Airfoil.app")

            self.assertEqual(result.returncode, 0)
            self.assertTrue(result.stdout.strip().endswith("Airfoil.app"))

    def test_declared_payload_rejects_absolute_traversal_and_ambiguity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for wrapper in ("release-1", "release-2"):
                app = root / wrapper / "Product.app" / "Contents"
                app.mkdir(parents=True)
                (app / "Info.plist").write_text("plist", encoding="utf-8")

            for expected in ("../Product.app", "/Applications/Product.app"):
                with self.subTest(expected=expected):
                    result = self.run_payload_helper(root, expected)
                    self.assertEqual(result.returncode, 0)
                    self.assertEqual(result.stdout.strip(), "")
                    self.assertIn("Rejected unsafe", result.stderr)

            result = self.run_payload_helper(root, "Product.app")
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "")
            self.assertIn("Ambiguous", result.stderr)

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

    def test_cleanup_uses_exact_prior_manifest_blob_without_prefix_listing(self):
        self.assertNotIn("az storage blob list", self.process)
        self.assertNotIn("--prefix", self.process)
        self.assertIn(
            'prior_catalog_url=$(printf \'%s\' "$prior_manifest"',
            self.process,
        )
        self.assertIn(
            'record_prior_blob_cleanup "$cleanup_prior_blob" "$new_blob_name"',
            self.process,
        )
        self.assertIn('safe_blob_leaf "$prior_blob"', self.process)

    def test_prefix_collision_names_cannot_be_cleanup_candidates(self):
        for shorter, longer in (
            ("battery", "battery_buddy"),
            ("geekbench", "geekbench_ai"),
            ("plex", "plex_media_server"),
        ):
            with self.subTest(shorter=shorter, longer=longer):
                self.assertNotIn(
                    f'--prefix "${{{shorter}}}_"',
                    self.process,
                )
        self.assertNotIn("existing_versions", self.process)

    def test_new_uploads_are_full_sha_content_addressed_and_immutable(self):
        self.assertIn(
            'new_blob_name="${app_name}_${version}_${file_hash}.pkg"',
            self.process,
        )
        self.assertIn('upload_immutable_blob()', self.process)
        self.assertIn('--overwrite false', self.process)
        self.assertNotIn('--overwrite true', self.process)
        self.assertIn('immutable_blob_exists "$blob_name"', self.process)

    def test_same_version_rebuild_does_not_touch_prior_blob_before_marker(self):
        marker = self.workflow.index("- name: Publish catalog state")
        before_marker = self.workflow[:marker]
        self.assertNotIn("az storage blob delete", before_marker)
        self.assertIn('prior_sha=$(printf', self.process)
        self.assertIn('prior_vendor_url=$(printf', self.process)
        self.assertIn('"$prior_vendor_url" != "$url"', self.process)

    def test_cross_account_reuse_requires_downloaded_sha_match(self):
        self.assertIn("prior_blob_sha_matches()", self.process)
        self.assertIn("verify_blob_sha \"$blob_name\"", self.process)
        self.assertIn("az storage blob download", self.process)
        self.assertIn('actual_sha=$(shasum -a 256', self.process)
        self.assertIn('[ "$actual_sha" = "$expected_lower" ]', self.process)
        self.assertIn(
            'prior_blob_sha_matches "$prior_is_configured" "$prior_blob" "$prior_sha"',
            self.process,
        )

    def test_configured_immutable_blob_can_use_name_sha_provenance(self):
        matcher = self.process.split("prior_blob_sha_matches() {", 1)[1].split(
            "upload_immutable_blob() {", 1
        )[0]
        matcher = "prior_blob_sha_matches() {" + matcher
        sha = "a" * 64
        script = (
            "verify_blob_sha() { return 1; }\n"
            + matcher
            + '\nprior_blob_sha_matches "$1" "$2" "$3"\n'
        )
        bash = shutil.which("bash")
        result = subprocess.run(
            [bash, "-c", script, "_", "true", f"app_1_{sha}.pkg", sha],
            check=False,
        )
        self.assertEqual(result.returncode, 0)

    def test_cross_account_blob_match_and_mismatch_follow_verifier(self):
        matcher = self.process.split("prior_blob_sha_matches() {", 1)[1].split(
            "upload_immutable_blob() {", 1
        )[0]
        matcher = "prior_blob_sha_matches() {" + matcher
        sha = "b" * 64
        bash = shutil.which("bash")
        for verifier_result, expected_code in ((0, 0), (1, 1)):
            with self.subTest(verifier_result=verifier_result):
                script = (
                    f"verify_blob_sha() {{ return {verifier_result}; }}\n"
                    + matcher
                    + '\nprior_blob_sha_matches "$1" "$2" "$3"\n'
                )
                result = subprocess.run(
                    [bash, "-c", script, "_", "false", "legacy.pkg", sha],
                    check=False,
                )
                self.assertEqual(result.returncode, expected_code)

    def test_reuse_requires_verified_full_source_identity(self):
        helper = self.process.split(
            "verified_source_identity_matches() {", 1
        )[1].split("configured_blob_leaf() {", 1)[0]
        helper = "verified_source_identity_matches() {" + helper
        bash = shutil.which("bash")
        verified_sha = "e" * 64
        cases = (
            (
                ("6.6.3,build1", "6.6.3,build1", verified_sha, verified_sha),
                0,
            ),
            (
                ("6.6.3,build1", "6.6.3,build2", verified_sha, verified_sha),
                1,
            ),
            (("14.4.9,14491", "14.4.9,14491", "no_check", "no_check"), 1),
            (("", "", "", ""), 1),
        )
        for arguments, expected_code in cases:
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [
                        bash,
                        "-c",
                        helper + '\nverified_source_identity_matches "$@"\n',
                        "_",
                        *arguments,
                    ],
                    check=False,
                )
                self.assertEqual(result.returncode, expected_code)

    def test_reuse_condition_reads_prior_and_current_source_identity(self):
        self.assertIn(".source_version // empty", self.process)
        self.assertIn(".source_sha256 // empty", self.process)
        self.assertIn("verified_source_identity_matches", self.process)

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
