import importlib.util
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github/workflows/build-app-packages.yml"
GENERATOR_PATH = ROOT / ".github/scripts/generate_supported_apps.py"
REFERER_VALIDATOR_PATH = (
    ROOT / ".github/scripts/validate_download_referer.py"
)

SPEC = importlib.util.spec_from_file_location("generate_supported_apps", GENERATOR_PATH)
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)


class CatalogPublicationContractTests(unittest.TestCase):
    def test_download_referer_validator_is_fail_closed(self):
        common = [
            os.fspath(REFERER_VALIDATOR_PATH),
            "--source",
            "https://www.jamovi.org/downloads/jamovi.dmg",
            "--homepage",
            "https://www.jamovi.org/",
        ]
        for referer, expected in (
            ("https://www.jamovi.org/download.html", 0),
            ("https://attacker.test/download.html", 1),
            ("https://www.jamovi.org/download.html?token=secret", 1),
            ("http://www.jamovi.org/download.html", 1),
            (
                "https://www.jamovi.org/download.html\r\nX-Injected: yes",
                1,
            ),
        ):
            with self.subTest(referer=referer):
                result = subprocess.run(
                    [shutil.which("python"), *common, "--referer", referer],
                    check=False,
                )
                self.assertEqual(result.returncode, expected)

    def test_progress_catalog_contains_every_currently_valid_manifest(self):
        supported = json.loads(
            (ROOT / "supported_apps.json").read_text(encoding="utf-8")
        )
        expected = {
            path.stem
            for path in (ROOT / "Apps").glob("*.json")
            if generator.is_publishable(
                json.loads(path.read_text(encoding="utf-8"))
            )
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

    def test_runtime_url_contract_rejects_query_fragment_and_userinfo(self):
        base = {
            "name": "Example",
            "version": "1",
            "bundleId": "com.example.app",
            "fileName": "example.dmg",
            "sha": "a" * 64,
        }
        cases = (
            ("https://example.test/app.dmg?token=x", "URL contains query"),
            ("https://example.test/app.dmg#fragment", "URL contains fragment"),
            ("https://user@example.test/app.dmg", "URL contains userinfo"),
            ("https://example.test:8443/app.dmg", "non-default URL port"),
        )
        for url, error in cases:
            with self.subTest(url=url):
                self.assertIn(
                    error,
                    generator.publication_errors(dict(base, url=url)),
                )

    def test_current_query_urls_are_blocked_until_repackaged(self):
        query_manifests = []
        for path in (ROOT / "Apps").glob("*.json"):
            app = json.loads(path.read_text(encoding="utf-8"))
            if not app.get("deprecated") and "?" in app.get("url", ""):
                query_manifests.append(path.name)
                self.assertIn(
                    "URL contains query",
                    generator.publication_errors(app),
                )
        self.assertIsInstance(query_manifests, list)

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

    def test_progress_generator_excludes_invalid_candidate(self):
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
                generator.generate_supported_apps(exclude_invalid=True)

            supported = json.loads(
                (root / "supported_apps.json").read_text(encoding="utf-8")
            )
            self.assertEqual(set(supported), set())


class WorkflowPackagingRegressionTests(unittest.TestCase):
    def test_remote_help_scraper_emits_verified_package_metadata(self):
        scraper = (
            ROOT / ".github/scripts/scrapers/remotehelp.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", scraper)
        self.assertIn("url_effective", scraper)
        self.assertIn("shasum -a 256", scraper)
        self.assertNotIn("pkgutil", scraper)
        self.assertIn("Microsoft_Remote_Help_", scraper)
        self.assertIn("_installer\\.pkg", scraper)
        self.assertIn('"artifact_kind": "pkg"', scraper)
        self.assertIn('"source_sha256": "$sha"', scraper)

    def test_remote_help_scraper_parses_effective_filename_on_ubuntu(self):
        bash = shutil.which("bash")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Apps").mkdir()
            bin_dir = root / "bin"
            bin_dir.mkdir()
            bash_env = root / "mock-env.sh"
            bash_env.write_text(
                "curl() {\n"
                "  local out=''\n"
                "  while [ $# -gt 0 ]; do\n"
                "    [ \"$1\" = '-o' ] && { out=\"$2\"; shift 2; continue; }\n"
                "    shift\n"
                "  done\n"
                "  printf pkg > \"$out\"\n"
                "  printf 'https://cdn.test/Microsoft_Remote_Help_1.2.3_installer.pkg'\n"
                "}\n"
                "shasum() { printf '%064d  file\\n' 1; }\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    bash,
                    str(ROOT / ".github/scripts/scrapers/remotehelp.sh"),
                ],
                cwd=root,
                env={
                    **os.environ,
                    "BASH_ENV": str(bash_env).replace("\\", "/"),
                    "MSYS2_ARG_CONV_EXCL": "*",
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads(
                (root / "Apps/remotehelp.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["version"], "1.2.3")
            self.assertEqual(manifest["bundleId"], "com.microsoft.remotehelp")

    def test_remote_help_scraper_fails_for_unexpected_filename(self):
        scraper = (
            ROOT / ".github/scripts/scrapers/remotehelp.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("Unexpected Remote Help installer filename", scraper)

    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.publisher = (
            ROOT / ".github/workflows/publish-catalog-state.yml"
        ).read_text(encoding="utf-8")
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
        self.assertIn('direct_pkg_file="$extract_dir/payload.pkg"', self.process)
        self.assertIn('pkg_file="$direct_pkg_file"', self.process)

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

    def test_declared_app_precedes_auxiliary_pkgs_in_dmg(self):
        dmg_path = self.workflow.split(
            'elif [ "$app_type" = "pkg_in_dmg" ]', 1
        )[1].split("else\n              # Download app", 1)[0]
        self.assertIn(
            'app_file=$(find_app_payload "${download_path}_mount" "$declared_app" "$source_app")',
            dmg_path,
        )
        self.assertIn(
            '[ -n "$app_file" ] || pkg_file=$(find_pkg_payload',
            dmg_path,
        )
        self.assertNotIn('find "${download_path}_mount" -name "*.pkg"', dmg_path)

    def test_extracted_bundle_id_must_match_catalog(self):
        self.assertIn("require_bundle_id_match()", self.process)
        self.assertIn("Extracted app bundle ID mismatch", self.process)
        self.assertIn("Declared package bundle ID mismatch", self.process)
        self.assertIn("Inner package bundle ID mismatch", self.process)
        self.assertIn("Extracted package bundle ID mismatch", self.process)
        self.assertNotIn("log_bundle_id_change", self.process)

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

    def test_rename_source_path_precedes_declared_path_and_rejects_ambiguity(self):
        self.assertIn("find_source_payload()", self.process)
        self.assertIn('find_source_payload "$root" "$source_expected"', self.process)
        self.assertIn("assert len(matches)<=1", self.process)
        self.assertIn("artifact_app_source", self.process)
        self.assertIn("artifact_pkg_source", self.process)

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

    def test_marker_is_terminal_and_workflow_never_deletes_blobs(self):
        marker = self.publisher.index("- name: Publish immutable catalog marker")
        self.assertNotIn("az storage blob delete", self.workflow)
        self.assertNotIn("superseded-blobs", self.workflow)
        self.assertNotIn("- name:", self.publisher[marker + 1 :])
        self.assertIn("git push origin HEAD:main", self.publisher[marker:])
        self.assertNotIn("Publish catalog state", self.workflow)

    def test_failed_or_unpublished_run_cannot_delete_old_blobs(self):
        require = self.workflow.index("- name: Require successful packaging")
        provenance = self.workflow.index("- name: Create publication provenance")
        self.assertLess(require, provenance)
        self.assertNotIn("az storage blob delete", self.workflow)

    def test_unmatched_partial_scope_fails_closed(self):
        find_step = self.workflow.split("- name: Find apps needing packaging", 1)[1]
        find_step = find_step.split("- name: Log in to Azure", 1)[0]
        self.assertIn(
            "::error::No manifest matched requested partial casks",
            find_step,
        )
        self.assertNotIn("falling back to a full build", find_step)

    def test_new_deprecation_tombstones_are_never_reverted(self):
        revert = self.workflow.split(
            "- name: Revert unselected package candidates", 1
        )[1].split("- name: Finalize catalog readiness", 1)[0]
        self.assertEqual(
            revert.count(".deprecated // false"),
            4,
        )

    def test_pkg_identity_excludes_nested_helpers_and_prefers_root(self):
        helper = self.process.split("extract_bundle_id_from_pkg() {", 1)[1].split(
            "require_bundle_id_match() {", 1
        )[0]
        self.assertIn('payload/Contents/Info.plist', helper)
        self.assertIn("select_package_identity.py", helper)
        selector = (
            ROOT / ".github/scripts/select_package_identity.py"
        ).read_text(encoding="utf-8")
        for excluded in ("Frameworks", "Sparkle", "LoginItems", "XPCServices", "Helpers"):
            self.assertIn(f'"{excluded}"', selector)
        self.assertIn("PackageInfo", selector)
        self.assertIn(
            '[ -z "$selected_id" ] && [ -n "$source_app" ]',
            helper,
        )
        self.assertIn(
            '[ -z "$selected_id" ] && [ -n "$declared_app" ]',
            helper,
        )

    def test_package_batches_are_bounded_resumable_and_deterministic(self):
        self.assertIn("max_packages:", self.workflow)
        self.assertIn('default: "25"', self.workflow)
        self.assertIn("package_candidates.py", self.workflow)
        self.assertIn("selected-packages.txt", self.workflow)
        self.assertIn("- name: Revert unselected package candidates", self.workflow)
        self.assertIn("Catalog progress committed without publication marker", self.workflow)
        self.assertIn("steps.catalog-ready.outputs.ready == 'true'", self.workflow)
        revert = self.workflow.split(
            "- name: Revert unselected package candidates", 1
        )[1].split("- name: Revert out-of-scope changes", 1)[0]
        self.assertEqual(
            revert.count("app|pkg_in_dmg|pkg_in_pkg"),
            2,
        )

    def test_no_prefix_listing_or_cleanup_journal_exists(self):
        self.assertNotIn("az storage blob list", self.process)
        self.assertNotIn("--prefix", self.process)
        self.assertNotIn("record_prior_blob_cleanup", self.process)

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
        upload = self.process.split("upload_immutable_blob() {", 1)[1].split(
            "ERROR_LOG=()", 1
        )[0]
        self.assertGreaterEqual(upload.count('verify_blob_sha "$blob_name" "$expected_sha"'), 3)

    def test_storage_base_must_match_account_and_container(self):
        self.assertIn(
            'expected_host="https://${AZURE_STORAGE_ACCOUNT}.blob.core.windows.net/${AZURE_STORAGE_CONTAINER}"',
            self.workflow,
        )

    def test_every_source_download_is_verified_before_use(self):
        self.assertIn("verify_source_file()", self.process)
        for message in (
            "Source ZIP SHA256 mismatch",
            "Source PKG SHA256 mismatch",
            "Source DMG SHA256 mismatch",
            "Direct source PKG SHA256 mismatch",
            "Source DMG.GZ SHA256 mismatch",
            "Source archive SHA256 mismatch",
        ):
            with self.subTest(message=message):
                self.assertIn(message, self.process)
        self.assertIn('download_user_agent=$(jq -r', self.process)
        self.assertIn('download_user_agent:-default', self.process)
        self.assertIn("Mozilla/5.0 (Macintosh;", self.process)
        self.assertIn("Homebrew/5.0.0 (Macintosh;", self.process)
        self.assertIn('download_referer=$(jq -r', self.process)
        self.assertIn(
            'referer_args=(--header "Referer: $download_referer")',
            self.process,
        )
        self.assertNotIn('referer_args=(--referer', self.process)
        self.assertIn(
            "Rejected unsafe or cross-origin download referer",
            self.process,
        )
        self.assertIn("validate_download_referer.py", self.process)
        self.assertNotIn("url_specs", self.process)
        verifier = self.process.split("verify_source_file() {", 1)[1].split(
            "MAX_SOURCE_BYTES=", 1
        )[0]
        self.assertIn('[ "$expected_sha" = "no_check" ]', verifier)
        self.assertIn(
            '[[ "$expected_sha" =~ ^[0-9a-fA-F]{64}$ ]] || return 1',
            verifier,
        )

    def test_remote_artifact_metadata_is_not_used_as_download_path(self):
        self.assertNotIn('${declared_pkg:-payload.pkg}', self.process)
        self.assertIn('source_path="$app_temp_dir/source.pkg"', self.process)

    def test_package_workspace_is_bounded_and_cleaned(self):
        self.assertIn("WORKSPACE_CAPACITY=10g", self.process)
        self.assertIn("HOST_WORKSPACE_KB=10485760", self.process)
        self.assertIn("require_host_workspace_capacity", self.process)
        self.assertIn("hdiutil create", self.process)
        self.assertIn("hdiutil attach", self.process)
        self.assertIn("-plist", self.process)
        self.assertIn('app_device="$devices"', self.process)
        self.assertIn('hdiutil detach "$app_device" -force', self.process)
        self.assertNotIn('hdiutil detach "$app_mount_dir"', self.process)
        self.assertIn("app_attached=true", self.process)
        self.assertIn(
            "refusing destructive cleanup",
            self.process,
        )
        self.assertIn("trap destroy_app_workspace EXIT", self.process)
        self.assertIn('package_path="$app_temp_dir/output.pkg"', self.process)
        self.assertNotIn('$HOME/Desktop/${app_name}', self.process)
        self.assertNotIn('${app_name}_extracted', self.process)
        self.assertNotIn('cd "$HOME/Desktop"', self.process)
        self.assertIn('[ -z "${app_mount_dir:-}" ] || rm -rf', self.process)
        self.assertIn('[ -z "${app_sparse_image:-}" ] || rm -f', self.process)
        self.assertGreaterEqual(self.process.count("destroy_app_workspace"), 4)

    def test_pkg_expansion_stays_inside_bounded_workspace(self):
        helper = self.process.split("extract_bundle_id_from_pkg() {", 1)[1].split(
            "require_bundle_id_match() {", 1
        )[0]
        self.assertIn('mktemp -d "$app_temp_dir/pkg-expand.XXXXXX"', helper)
        self.assertIn('require_extracted_quota "$expand_dir"', helper)
        self.assertNotIn("mktemp -d 2>/dev/null", helper)

    def test_resource_guards_bound_downloads_archives_and_disk(self):
        self.assertIn("timeout-minutes: 180", self.workflow)
        self.assertIn("MAX_SOURCE_BYTES=3221225472", self.process)
        self.assertIn("MAX_EXPANDED_BYTES=6442450944", self.process)
        self.assertIn("MIN_FREE_KB=2097152", self.process)
        self.assertIn("--connect-timeout 30", self.process)
        self.assertIn("--max-time 300", self.process)
        self.assertIn("--max-filesize \"$MAX_SOURCE_BYTES\"", self.process)
        self.assertIn("require_archive_quota", self.process)
        self.assertIn("archive_quota.py", self.process)
        self.assertIn('[ "${probe_status:-1}" -eq 0 ] || return 1', self.process)
        self.assertIn('[[ "$expanded" =~ ^[0-9]+$ ]] || return 1', self.process)
        self.assertIn("require_free_disk", self.process)
        self.assertIn('package_bytes=$(stat -f %z "$package_path")', self.process)

    def test_streaming_blob_readback_has_no_second_disk_copy(self):
        helper = self.process.split("verify_blob_sha() {", 1)[1].split(
            "prior_blob_sha_matches() {", 1
        )[0]
        self.assertIn("az account get-access-token", helper)
        self.assertIn("Authorization: Bearer", helper)
        self.assertIn("| shasum -a 256", helper)
        self.assertNotIn("az storage blob download", helper)
        self.assertNotIn("mktemp", helper)

    def test_archive_quota_algorithms_measure_real_uncompressed_bytes(self):
        spec = importlib.util.spec_from_file_location(
            "archive_quota",
            ROOT / ".github/scripts/archive_quota.py",
        )
        archive_quota = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(archive_quota)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload"
            payload.write_bytes(b"x" * 4096)
            zip_path = root / "payload.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.write(payload, "payload")
            tar_path = root / "payload.tar.gz"
            with tarfile.open(tar_path, "w:gz") as archive:
                archive.add(payload, arcname="payload")
            self.assertEqual(
                archive_quota.archive_totals(
                    zip_path,
                    "zip",
                    8192,
                    10,
                ),
                (4096, 1),
            )
            self.assertEqual(
                archive_quota.archive_totals(
                    tar_path,
                    "tar.gz",
                    8192,
                    10,
                ),
                (4096, 1),
            )

    def test_archive_quota_aborts_on_member_limit(self):
        spec = importlib.util.spec_from_file_location(
            "archive_quota",
            ROOT / ".github/scripts/archive_quota.py",
        )
        archive_quota = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(archive_quota)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "many.tar.gz"
            payload = Path(directory) / "item"
            payload.write_bytes(b"x")
            with tarfile.open(path, "w:gz") as archive:
                for index in range(5):
                    archive.add(payload, arcname=f"item-{index}")
            with self.assertRaises(archive_quota.QuotaExceeded):
                archive_quota.archive_totals(
                    path,
                    "tar.gz",
                    1024,
                    2,
                )

    def test_zip_data_descriptor_and_zip64_are_supported(self):
        spec = importlib.util.spec_from_file_location(
            "archive_quota_descriptor",
            ROOT / ".github/scripts/archive_quota.py",
        )
        archive_quota = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(archive_quota)

        class NonSeekable(BytesIO):
            def seekable(self):
                return False

            def seek(self, *args, **kwargs):
                raise OSError("not seekable")

        descriptor = NonSeekable()
        with zipfile.ZipFile(descriptor, "w") as archive:
            archive.writestr("descriptor.txt", b"x" * 1024)

        with tempfile.TemporaryDirectory() as directory:
            descriptor_path = Path(directory) / "descriptor.zip"
            descriptor_path.write_bytes(descriptor.getvalue())
            self.assertEqual(
                archive_quota.archive_totals(
                    descriptor_path,
                    "zip",
                    2048,
                    10,
                ),
                (1024, 1),
            )

            zip64_path = Path(directory) / "zip64.zip"
            with zipfile.ZipFile(zip64_path, "w", allowZip64=True) as archive:
                with archive.open("zip64.txt", "w", force_zip64=True) as item:
                    item.write(b"y" * 2048)
            self.assertEqual(
                archive_quota.archive_totals(
                    zip64_path,
                    "zip",
                    4096,
                    10,
                ),
                (2048, 1),
            )

    def test_zip_central_directory_malformed_and_early_limits_fail(self):
        spec = importlib.util.spec_from_file_location(
            "archive_quota_malformed",
            ROOT / ".github/scripts/archive_quota.py",
        )
        archive_quota = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(archive_quota)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "many.zip"
            with zipfile.ZipFile(path, "w") as archive:
                for index in range(5):
                    archive.writestr(f"item-{index}", b"x")
            with self.assertRaises(archive_quota.QuotaExceeded):
                archive_quota.archive_totals(path, "zip", 1024, 2)

            truncated = Path(directory) / "truncated.zip"
            truncated.write_bytes(path.read_bytes()[:-10])
            with self.assertRaises(ValueError):
                archive_quota.archive_totals(truncated, "zip", 1024, 10)

    def test_same_version_rebuild_does_not_touch_prior_blob_before_marker(self):
        self.assertNotIn("az storage blob delete", self.workflow)
        self.assertIn('prior_sha=$(printf', self.process)
        self.assertIn('prior_vendor_url=$(printf', self.process)
        self.assertIn('"$prior_vendor_url" != "$url"', self.process)

    def test_cross_account_reuse_requires_downloaded_sha_match(self):
        self.assertIn("prior_blob_sha_matches()", self.process)
        self.assertIn("verify_blob_sha \"$blob_name\"", self.process)
        self.assertIn("az account get-access-token", self.process)
        self.assertIn("Authorization: Bearer", self.process)
        self.assertNotIn("az storage blob download", self.process)
        self.assertIn('actual_sha=$(curl -fLsS', self.process)
        self.assertIn('| shasum -a 256', self.process)
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
        process = self.workflow.index("- name: Process apps")
        final = self.workflow.index("- name: Finalize catalog readiness", process)
        provenance = self.workflow.index("- name: Create publication provenance")
        self.assertLess(process, final)
        self.assertLess(final, provenance)
        readiness = self.workflow[final:provenance]
        self.assertIn("generate_supported_apps.py", readiness)
        self.assertIn("--exclude-invalid", readiness)


if __name__ == "__main__":
    unittest.main()
