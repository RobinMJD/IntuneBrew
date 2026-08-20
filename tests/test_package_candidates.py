import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "package_candidates",
    ROOT / ".github/scripts/package_candidates.py",
)
package_candidates = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package_candidates)


class PackageCandidateTests(unittest.TestCase):
    def setUp(self):
        self.base = "https://account.blob.core.windows.net/pkg"
        self.prior = {
            "type": "app",
            "url": f"{self.base}/app_1_{'a' * 64}.pkg",
            "sha": "a" * 64,
            "source_version": "1,100",
            "source_sha256": "b" * 64,
            "vendor_url": "https://example.test/app.zip",
        }
        self.current = dict(self.prior, url="https://example.test/app.zip")

    def test_stable_verified_package_is_not_a_candidate(self):
        self.assertFalse(
            package_candidates.needs_package(
                self.current,
                self.prior,
                self.base,
            )
        )

    def test_source_change_and_no_check_are_candidates(self):
        changed = dict(self.current, source_version="1,101")
        self.assertTrue(
            package_candidates.needs_package(changed, self.prior, self.base)
        )
        no_check = dict(self.current, source_sha256="no_check")
        self.assertTrue(
            package_candidates.needs_package(no_check, self.prior, self.base)
        )

    def test_oldest_checked_candidate_sorts_first_for_resume(self):
        older = ("z", {}, {"source_checked_at": "2026-01-01T00:00:00Z"})
        newer = ("a", {}, {"source_checked_at": "2026-08-01T00:00:00Z"})
        self.assertEqual(
            sorted([newer, older], key=package_candidates.sort_key)[0][0],
            "z",
        )


class SourceBootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "bootstrap_source_identity",
            ROOT / ".github/scripts/bootstrap_source_identity.py",
        )
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_only_valid_matching_private_package_is_bootstrapped(self):
        app = {
            "type": "app",
            "url": "https://account.blob.core.windows.net/pkg/app.pkg",
            "version": "1",
            "vendor_url": "https://example.test/app.zip",
            "bundleId": "com.example.app",
        }
        cask = {
            "version": "1,100",
            "url": "https://example.test/app.zip",
            "sha256": "a" * 64,
        }
        self.assertTrue(self.module.bootstrap_manifest(app, cask))
        self.assertEqual(app["source_version"], "1,100")
        self.assertEqual(app["source_sha256"], "a" * 64)
        self.assertFalse(
            self.module.bootstrap_manifest(
                dict(app),
                dict(cask, sha256="no_check"),
            )
        )

    def test_no_check_is_marked_pending_not_verified(self):
        app = {
            "type": "app",
            "url": "https://account.blob.core.windows.net/pkg/app.pkg",
            "version": "1",
            "vendor_url": "https://example.test/app.zip",
        }
        cask = {
            "version": "1,100",
            "url": "https://example.test/app.zip",
            "sha256": "no_check",
        }
        self.assertTrue(self.module.mark_unverified_source(app, cask))
        self.assertEqual(app["source_sha256"], "no_check")
        self.assertTrue(app["packaging_pending"])
        self.assertNotIn("source_sha256_provenance", app)
