import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "collect_app_info",
    ROOT / ".github/scripts/collect_app_info.py",
)
collect_app_info = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collect_app_info)


def cask_response(payload=None, status_code=200):
    """Build a fake requests response for one cask JSON fetch."""
    response = Mock(status_code=status_code)
    if status_code >= 400:
        response.raise_for_status.side_effect = collect_app_info.requests.HTTPError(
            response=response
        )
    else:
        response.raise_for_status.return_value = None
        response.json.return_value = payload
    return response


class CountingSession:
    """Stands in for the module-level session and records every URL it is asked for."""

    def __init__(self, responses):
        self.responses = responses
        self.requested = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return self.responses[url]


class CollectAppInfoTests(unittest.TestCase):
    def setUp(self):
        collect_app_info.cask_cache.clear()

    def tearDown(self):
        collect_app_info.cask_cache.clear()

    def test_homebrew_404_is_reported_as_removed_cask(self):
        response = Mock(status_code=404)
        response.raise_for_status.side_effect = collect_app_info.requests.HTTPError(response=response)

        with patch.object(collect_app_info, "cask_session", Mock(get=Mock(return_value=response))):
            with self.assertRaises(collect_app_info.CaskUnavailableError) as context:
                collect_app_info.get_homebrew_app_info(
                    "https://formulae.brew.sh/api/cask/removed-app.json"
                )

        self.assertEqual(context.exception.cask_token, "removed-app")
        self.assertIsNone(context.exception.display_name)
        self.assertEqual(context.exception.reason, "cask removed from Homebrew")

    def test_non_404_homebrew_error_is_not_treated_as_deprecation(self):
        response = Mock(status_code=503)
        response.raise_for_status.side_effect = collect_app_info.requests.HTTPError(response=response)

        with patch.object(collect_app_info, "cask_session", Mock(get=Mock(return_value=response))):
            with self.assertRaises(collect_app_info.requests.HTTPError):
                collect_app_info.get_homebrew_app_info(
                    "https://formulae.brew.sh/api/cask/temporarily-unavailable.json"
                )

    def test_disabled_cask_preserves_display_name_and_token(self):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "name": ["Example App"],
            "deprecated": False,
            "disabled": True,
            "disable_reason": "discontinued",
        }

        with patch.object(collect_app_info, "cask_session", Mock(get=Mock(return_value=response))):
            with self.assertRaises(collect_app_info.CaskUnavailableError) as context:
                collect_app_info.get_homebrew_app_info(
                    "https://formulae.brew.sh/api/cask/example-app.json"
                )

        self.assertEqual(context.exception.display_name, "Example App")
        self.assertEqual(context.exception.cask_token, "example-app")
        self.assertEqual(
            context.exception.reason,
            "disabled in Homebrew: discontinued",
        )

    def test_removed_cask_marks_matching_app_deprecated(self):
        with tempfile.TemporaryDirectory() as directory:
            app_path = Path(directory) / "vmware_fusion.json"
            app_path.write_text(
                json.dumps({"name": "VMware Fusion", "version": "13.6.3"}),
                encoding="utf-8",
            )

            changed = collect_app_info.mark_app_deprecated(
                directory,
                display_name=None,
                reason="cask removed from Homebrew",
                cask_token="vmware-fusion",
            )

            self.assertTrue(changed)
            app_data = json.loads(app_path.read_text(encoding="utf-8"))
            self.assertTrue(app_data["deprecated"])
            self.assertEqual(
                app_data["deprecation_reason"],
                "cask removed from Homebrew",
            )
            self.assertEqual(app_data["homebrew_cask"], "vmware-fusion")

    def test_stored_cask_provenance_resolves_nonstandard_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            app_path = Path(directory) / "custom_filename.json"
            app_path.write_text(
                json.dumps(
                    {
                        "name": "Different Display Name",
                        "homebrew_cask": "source-token",
                    }
                ),
                encoding="utf-8",
            )

            resolved = collect_app_info.find_app_file(
                directory,
                cask_token="source-token",
            )

            self.assertEqual(resolved, str(app_path))

    def test_artifact_kind_uses_url_extension_not_file_description(self):
        self.assertEqual(
            collect_app_info.get_artifact_kind(
                "https://example.test/download.pkg?signature=xar"
            ),
            "pkg",
        )
        self.assertEqual(
            collect_app_info.get_artifact_kind(
                "https://example.test/compressed.dmg?encoding=lzfse"
            ),
            "dmg",
        )
        self.assertEqual(
            collect_app_info.get_artifact_kind("https://example.test/app.tgz"),
            "archive",
        )
        self.assertEqual(
            collect_app_info.get_archive_format("https://example.test/app.tgz"),
            "tar.gz",
        )
        self.assertEqual(
            collect_app_info.get_artifact_kind(
                "https://example.test/TOSHIBA_ColorMFP.dmg.gz"
            ),
            "dmg_gzip",
        )

    def test_declared_app_wins_over_nested_helper_apps(self):
        artifacts = collect_app_info.get_installable_artifacts(
            {
                "artifacts": [
                    {"app": ["Product.app", {"target": "Renamed.app"}]},
                    {"zap": [{"trash": "~/Library/Caches/Product"}]},
                ]
            }
        )

        self.assertEqual(artifacts["app"], "Product.app")
        self.assertIsNone(artifacts["pkg"])

    def test_archive_pkg_artifact_is_discoverable(self):
        artifacts = collect_app_info.get_installable_artifacts(
            {"artifacts": [{"pkg": ["Installer.pkg"]}]}
        )

        self.assertEqual(artifacts["pkg"], "Installer.pkg")

    def test_declared_archive_app_is_automatically_queued_for_packaging(self):
        url = "https://formulae.brew.sh/api/cask/example.json"
        payload = {
            "name": ["Example"],
            "desc": "Example app",
            "version": "1.0",
            "url": "https://example.test/Example.zip",
            "sha256": "a" * 64,
            "homepage": "https://example.test/",
            "artifacts": [{"app": ["Example.app"]}],
        }

        with patch.dict(collect_app_info.cask_cache, {url: payload}, clear=True):
            app_info = collect_app_info.get_homebrew_app_info(url)

        self.assertEqual(app_info["type"], "app")
        self.assertEqual(app_info["artifact_app"], "Example.app")
        self.assertEqual(app_info["sha"], "a" * 64)

    def test_full_source_identity_is_persisted_without_normalization(self):
        url = "https://formulae.brew.sh/api/cask/keybase.json"
        payload = {
            "name": ["Keybase"],
            "desc": "Encrypted messaging",
            "version": "6.6.3,20260603142618,f60f2ff97e",
            "url": "https://example.test/Keybase.dmg",
            "sha256": "no_check",
            "homepage": "https://example.test/",
            "artifacts": [{"app": ["Keybase.app"]}],
        }

        with patch.dict(collect_app_info.cask_cache, {url: payload}, clear=True):
            app_info = collect_app_info.get_homebrew_app_info(
                url,
                needs_packaging=True,
            )

        self.assertEqual(app_info["version"], "6.6.3")
        self.assertEqual(
            app_info["source_version"],
            "6.6.3,20260603142618,f60f2ff97e",
        )
        self.assertEqual(app_info["source_sha256"], "no_check")

    def test_direct_hash_reuse_requires_verified_source_identity(self):
        existing = {
            "source_version": "1.0,100",
            "source_sha256": "a" * 64,
            "url": "https://example.test/app.dmg",
            "sha": "b" * 64,
        }
        current = dict(existing)
        self.assertTrue(collect_app_info.can_reuse_source_hash(existing, current))

        for changed in (
            {"source_version": "1.0,101"},
            {"source_sha256": "no_check"},
            {"source_sha256": ""},
            {"url": "https://example.test/app-v2.dmg"},
        ):
            with self.subTest(changed=changed):
                candidate = dict(current, **changed)
                self.assertFalse(
                    collect_app_info.can_reuse_source_hash(existing, candidate)
                )

    def test_direct_manifest_bootstrap_reuses_authoritative_source_hash(self):
        source_sha = "c" * 64
        existing = {
            "version": "1.0",
            "url": "https://example.test/app.dmg",
            "sha": source_sha,
        }
        current = {
            "version": "1.0",
            "url": "https://example.test/app.dmg",
            "source_version": "1.0,100",
            "source_sha256": source_sha,
        }
        self.assertTrue(collect_app_info.can_reuse_source_hash(existing, current))
        current["type"] = "app"
        self.assertFalse(collect_app_info.can_reuse_source_hash(existing, current))

    def test_downloaded_source_hash_mismatch_is_rejected(self):
        app_info = {
            "name": "Example",
            "homebrew_cask": "example",
            "url": "https://example.test/app.dmg",
            "source_sha256": "a" * 64,
        }
        with patch.object(
            collect_app_info,
            "calculate_file_hash",
            return_value="b" * 64,
        ):
            with self.assertRaises(collect_app_info.SourceHashMismatchError) as context:
                collect_app_info.calculate_verified_source_hash(app_info)
        self.assertEqual(context.exception.app_name, "Example")
        self.assertEqual(context.exception.cask_token, "example")
        self.assertEqual(context.exception.expected, "a" * 64)
        self.assertEqual(context.exception.actual, "b" * 64)
        self.assertEqual(context.exception.url, app_info["url"])
        self.assertNotIn("sha", app_info)
        self.assertTrue(app_info["source_hash_mismatch"])
        self.assertFalse(
            collect_app_info.can_reuse_source_hash(
                {
                    "source_version": "1",
                    "source_sha256": "a" * 64,
                    "url": app_info["url"],
                    "sha": "a" * 64,
                },
                dict(app_info, source_version="1"),
            )
        )

    def test_downloaded_no_check_source_keeps_calculated_hash(self):
        app_info = {
            "name": "Example",
            "url": "https://example.test/app.dmg",
            "source_sha256": "no_check",
        }
        with patch.object(
            collect_app_info,
            "calculate_file_hash",
            return_value="b" * 64,
        ):
            self.assertEqual(
                collect_app_info.calculate_verified_source_hash(app_info),
                "b" * 64,
            )

    def test_wireshark_declared_app_takes_precedence_over_auxiliary_pkgs(self):
        url = "https://formulae.brew.sh/api/cask/wireshark-app.json"
        payload = {
            "name": ["Wireshark"],
            "desc": "Packet analyzer",
            "version": "4.0",
            "url": "https://example.test/Wireshark.dmg",
            "sha256": "f" * 64,
            "homepage": "https://example.test/",
            "artifacts": [
                {"app": ["Wireshark.app"]},
                {"pkg": ["Install ChmodBPF.pkg"]},
            ],
        }
        with patch.dict(collect_app_info.cask_cache, {url: payload}, clear=True):
            app_info = collect_app_info.get_homebrew_app_info(
                url,
                is_pkg_in_dmg=True,
            )

        self.assertEqual(app_info["type"], "app")
        self.assertEqual(app_info["artifact_app"], "Wireshark.app")
        self.assertEqual(app_info["artifact_pkg"], "Install ChmodBPF.pkg")

    def test_missing_tombstone_for_unavailable_configured_cask_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "no manifest tombstone"):
                collect_app_info.require_deprecation_tombstone(
                    directory,
                    "Bootstrap",
                    "installer-only",
                    "bootstrap",
                )

    def test_quarantined_cask_is_rejected_with_structured_identity(self):
        url = "https://formulae.brew.sh/api/cask/visual-paradigm.json"
        payload = {
            "name": ["Visual Paradigm"],
            "version": "18.0",
            "url": "https://example.test/Visual_Paradigm.dmg",
        }
        quarantine = {
            "visual-paradigm": {
                "reason": "Source integrity quarantine: reviewed mismatch"
            }
        }
        with (
            patch.dict(collect_app_info.cask_cache, {url: payload}, clear=True),
            patch.object(
                collect_app_info,
                "load_source_integrity_quarantine",
                return_value=quarantine,
            ),
            self.assertRaises(
                collect_app_info.SourceIntegrityQuarantineError
            ) as context,
        ):
            collect_app_info.get_homebrew_app_info(url)

        self.assertEqual(context.exception.display_name, "Visual Paradigm")
        self.assertEqual(context.exception.cask_token, "visual-paradigm")
        self.assertEqual(
            context.exception.reason,
            "Source integrity quarantine: reviewed mismatch",
        )

    def test_quarantine_can_create_a_missing_tombstone(self):
        with tempfile.TemporaryDirectory() as directory:
            collect_app_info.require_deprecation_tombstone(
                directory,
                "Visual Paradigm",
                "Source integrity quarantine: reviewed mismatch",
                "visual-paradigm",
                allow_create=True,
            )

            tombstone_path = Path(directory) / "visual_paradigm.json"
            tombstone = json.loads(tombstone_path.read_text(encoding="utf-8"))
            self.assertTrue(tombstone["deprecated"])
            self.assertEqual(tombstone["homebrew_cask"], "visual-paradigm")
            self.assertEqual(
                tombstone["deprecation_reason"],
                "Source integrity quarantine: reviewed mismatch",
            )

    def test_quarantine_refuses_ownerless_existing_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "visual_paradigm.json"
            original = json.dumps(
                {"name": "Visual Paradigm", "version": "18.0"}
            )
            manifest_path.write_text(original, encoding="utf-8")

            with self.assertRaisesRegex(
                RuntimeError,
                "not conclusively owned",
            ):
                collect_app_info.require_deprecation_tombstone(
                    directory,
                    "Visual Paradigm",
                    "Source integrity quarantine: reviewed mismatch",
                    "visual-paradigm",
                    allow_create=True,
                )

            self.assertEqual(
                manifest_path.read_text(encoding="utf-8"),
                original,
            )

    def test_gzip_dmg_pkg_is_queued_for_safe_packaging(self):
        url = "https://formulae.brew.sh/api/cask/toshiba-color-mfp.json"
        payload = {
            "name": ["TOSHIBA ColorMFP"],
            "desc": "Printer driver",
            "version": "7.119.4.0,21838",
            "url": "https://example.test/TOSHIBA_ColorMFP.dmg.gz",
            "sha256": "d" * 64,
            "homepage": "https://example.test/",
            "artifacts": [{"pkg": ["TOSHIBA ColorMFP.pkg"]}],
        }

        with patch.dict(collect_app_info.cask_cache, {url: payload}, clear=True):
            app_info = collect_app_info.get_homebrew_app_info(url, is_pkg=True)

        self.assertEqual(app_info["artifact_kind"], "dmg_gzip")
        self.assertEqual(app_info["artifact_pkg"], "TOSHIBA ColorMFP.pkg")
        self.assertEqual(app_info["type"], "app")

    def test_bundle_id_override_fills_cask_without_detectable_id(self):
        url = "https://formulae.brew.sh/api/cask/cmtrace-open.json"
        payload = {
            "name": ["CMTrace Open"],
            "desc": "Log viewer",
            "version": "1.5.2",
            "url": "https://example.test/CMTrace.dmg",
            "sha256": "b" * 64,
            "homepage": "https://example.test/",
            "artifacts": [{"app": ["CMTrace Open.app"]}],
        }

        with patch.dict(collect_app_info.cask_cache, {url: payload}, clear=True):
            app_info = collect_app_info.get_homebrew_app_info(url)

        self.assertEqual(app_info["bundleId"], "com.cmtrace.open")

    def test_bundle_id_override_wins_over_unrelated_launchctl(self):
        url = "https://formulae.brew.sh/api/cask/hopper-disassembler.json"
        payload = {
            "name": ["Hopper Disassembler"],
            "desc": "Disassembler",
            "version": "6.5",
            "url": "https://example.test/Hopper.dmg",
            "sha256": "a" * 64,
            "homepage": "https://example.test/",
            "artifacts": [
                {"uninstall": [{"launchctl": "com.cryptic-apps.ExternalAPI"}]},
                {"app": ["Hopper Disassembler.app"]},
            ],
        }
        with patch.dict(collect_app_info.cask_cache, {url: payload}, clear=True):
            app_info = collect_app_info.get_homebrew_app_info(url)
        self.assertEqual(
            app_info["bundleId"],
            "com.cryptic-apps.hopper-web-4",
        )

    def test_existing_null_bundle_id_cannot_replace_fresh_override(self):
        existing = {"bundleId": None}
        fresh = {
            "bundleId": "com.cmtrace.open",
            "bundleId_source": "override",
        }
        collect_app_info.merge_fresh_bundle_id(existing, fresh)
        self.assertEqual(existing["bundleId"], "com.cmtrace.open")

    def test_transient_null_bundle_lookup_preserves_existing_value(self):
        fresh = {"bundleId": None}
        existing = {"bundleId": "com.example.valid"}
        collect_app_info.preserve_existing_bundle_id(fresh, existing)
        self.assertEqual(fresh["bundleId"], "com.example.valid")

    def test_bundle_id_precedence_matrix(self):
        cases = (
            (
                {"bundleId": "com.figma.Desktop"},
                {"bundleId": "com.figma.agent", "bundleId_source": "heuristic"},
                ("com.figma.Desktop", "legacy"),
            ),
            (
                {"bundleId": "com.figma.Desktop"},
                {"bundleId": "com.override", "bundleId_source": "override"},
                ("com.override", "override"),
            ),
            (
                {"bundleId": "com.example.*"},
                {"bundleId": "com.heuristic", "bundleId_source": "heuristic"},
                ("com.heuristic", "heuristic"),
            ),
            (
                {"bundleId": "com.example.*"},
                {"bundleId": "other.*", "bundleId_source": "heuristic"},
                (None, "missing"),
            ),
        )
        for existing, fresh, expected in cases:
            with self.subTest(existing=existing, fresh=fresh):
                self.assertEqual(
                    collect_app_info.select_bundle_id(existing, fresh),
                    expected,
                )

    def test_metadata_sync_cannot_downgrade_stored_bundle_authority(self):
        existing = {
            "bundleId": "com.figma.Desktop",
            "bundleId_source": "package",
        }
        fresh = {
            "bundleId": "com.figma.agent",
            "bundleId_source": "heuristic",
            "artifact_kind": "archive",
        }
        collect_app_info.sync_artifact_metadata(existing, fresh)
        collect_app_info.merge_fresh_bundle_id(existing, fresh)
        self.assertEqual(existing["bundleId"], "com.figma.Desktop")
        self.assertEqual(existing["bundleId_source"], "package")

    def test_generic_metadata_sync_never_touches_bundle_provenance(self):
        existing = {
            "bundleId": "com.example.app",
            "bundleId_source": "stored",
        }
        collect_app_info.sync_artifact_metadata(
            existing,
            {
                "bundleId": "com.example.helper",
                "bundleId_source": "heuristic",
                "artifact_kind": "dmg",
            },
        )
        self.assertEqual(existing["bundleId"], "com.example.app")
        self.assertEqual(existing["bundleId_source"], "stored")

    def test_wildcard_bundle_ids_are_never_concrete(self):
        self.assertFalse(collect_app_info.is_concrete_bundle_id("com.example.*"))
        self.assertFalse(collect_app_info.is_concrete_bundle_id("org.r-project?"))
        self.assertTrue(collect_app_info.is_concrete_bundle_id("com.figma.Desktop"))

    def test_extensionless_archive_override_is_persisted(self):
        url = "https://formulae.brew.sh/api/cask/postman.json"
        payload = {
            "name": ["Postman"],
            "desc": "API client",
            "version": "1.0",
            "url": "https://example.test/download/arm64",
            "sha256": "c" * 64,
            "homepage": "https://example.test/",
            "artifacts": [{"app": ["Postman.app"]}],
        }

        with patch.dict(collect_app_info.cask_cache, {url: payload}, clear=True):
            app_info = collect_app_info.get_homebrew_app_info(url)

        self.assertEqual(app_info["artifact_kind"], "archive")
        self.assertEqual(app_info["archive_format"], "zip")
        self.assertEqual(app_info["type"], "app")

    def test_rename_source_paths_and_browser_user_agent_are_persisted(self):
        cases = (
            (
                "ecamm-live",
                [{"app": ["Ecamm/Ecamm Live.app"]}],
                "artifact_app_source",
                "Ecamm*/Ecamm Live.app",
            ),
            (
                "loupedeck",
                [{"pkg": ["Installer.pkg"]}],
                "artifact_pkg_source",
                "LoupedeckInstaller.pkg",
            ),
        )
        for token, artifacts, key, expected in cases:
            with self.subTest(token=token):
                url = f"https://formulae.brew.sh/api/cask/{token}.json"
                payload = {
                    "name": [token],
                    "desc": token,
                    "version": "1",
                    "url": "https://example.test/app.zip",
                    "sha256": "a" * 64,
                    "homepage": "https://example.test/",
                    "artifacts": artifacts,
                    "url_specs": {},
                }
                with patch.dict(
                    collect_app_info.cask_cache,
                    {url: payload},
                    clear=True,
                ):
                    info = collect_app_info.get_homebrew_app_info(
                        url,
                        needs_packaging=True,
                    )
                self.assertEqual(info[key], expected)
                self.assertEqual(info["download_user_agent"], "default")

        url = "https://formulae.brew.sh/api/cask/ddpm.json"
        payload["name"] = ["DDPM"]
        payload["artifacts"] = [{"pkg": ["DDPM_Installer.pkg"]}]
        payload["url_specs"] = {"user_agent": ":browser", "headers": {"X": "secret"}}
        with patch.dict(collect_app_info.cask_cache, {url: payload}, clear=True):
            info = collect_app_info.get_homebrew_app_info(url, needs_packaging=True)
        self.assertEqual(info["download_user_agent"], "browser")
        self.assertNotIn("headers", info)

    def test_warp_preserves_current_homebrew_source_identity(self):
        api_url = "https://formulae.brew.sh/api/cask/warp.json"
        source_url = (
            "https://app.warp.dev/download/brew?"
            "version=v0.2026.08.19.08.15.stable_01"
        )
        payload = {
            "token": "warp",
            "name": ["Warp"],
            "desc": "Rust-based terminal",
            "version": "0.2026.08.19.08.15.stable_01",
            "url": source_url,
            "sha256": (
                "464dedeeb39c5b9db9f5af1698d438c04bc22be664e91bfa01fa8d77e934f617"
            ),
            "homepage": "https://www.warp.dev/terminal",
            "artifacts": [{"app": ["Warp.app"]}],
            "url_specs": {},
        }

        with patch.dict(
            collect_app_info.cask_cache,
            {api_url: payload},
            clear=True,
        ):
            app_info = collect_app_info.get_homebrew_app_info(api_url)

        self.assertEqual(app_info["url"], source_url)
        self.assertEqual(app_info["vendor_url"], source_url)
        self.assertEqual(
            app_info["source_version"],
            "0.2026.08.19.08.15.stable_01",
        )
        self.assertEqual(app_info["source_sha256"], payload["sha256"])
        self.assertEqual(app_info["artifact_kind"], "dmg")
        self.assertEqual(app_info["type"], "app")
        self.assertEqual(app_info["download_user_agent"], "homebrew")
        self.assertNotIn("releases.warp.dev", json.dumps(app_info))

    def test_only_safe_same_origin_referers_are_persisted(self):
        api_url = "https://formulae.brew.sh/api/cask/jamovi.json"
        base_payload = {
            "token": "jamovi",
            "name": ["jamovi"],
            "desc": "Statistical software",
            "version": "28.2.0.0",
            "url": "https://www.jamovi.org/downloads/jamovi.dmg",
            "sha256": "d" * 64,
            "homepage": "https://www.jamovi.org/",
            "artifacts": [{"app": ["jamovi.app"]}],
        }
        cases = (
            ("https://www.jamovi.org/download.html", True),
            ("https://www.jamovi.org:443/download.html", True),
            ("http://www.jamovi.org/download.html", False),
            ("https://user@www.jamovi.org/download.html", False),
            ("https://www.jamovi.org:8443/download.html", False),
            ("https://www.jamovi.org/download.html?token=secret", False),
            ("https://www.jamovi.org/download.html#section", False),
            ("https://attacker.test/download.html", False),
            (
                "https://www.jamovi.org/download.html\r\nX-Injected: yes",
                False,
            ),
            ("https://www.jamovi.org/download.html\tX-Injected: yes", False),
        )

        for referer, accepted in cases:
            with self.subTest(referer=referer):
                payload = dict(
                    base_payload,
                    url_specs={
                        "referer": referer,
                        "headers": {"Authorization": "secret"},
                        "cookies": {"session": "secret"},
                    },
                )
                with patch.dict(
                    collect_app_info.cask_cache,
                    {api_url: payload},
                    clear=True,
                ):
                    app_info = collect_app_info.get_homebrew_app_info(api_url)
                if accepted:
                    self.assertEqual(app_info["download_referer"], referer)
                else:
                    self.assertNotIn("download_referer", app_info)
                self.assertNotIn("headers", app_info)
                self.assertNotIn("cookies", app_info)

    def test_jamovi_current_metadata_persists_approved_referer(self):
        api_url = "https://formulae.brew.sh/api/cask/jamovi.json"
        source_url = (
            "https://www.jamovi.org/downloads/"
            "jamovi-28.2.0.0-macos-arm64.dmg"
        )
        payload = {
            "token": "jamovi",
            "name": ["jamovi"],
            "desc": "Statistical software",
            "version": "28.2.0.0",
            "url": source_url,
            "sha256": (
                "d50b4040b4c4040d842c48c58b39d05a1dac0eef879f5adc189348456f19991b"
            ),
            "homepage": "https://www.jamovi.org/",
            "artifacts": [{"app": ["jamovi.app"]}],
            "url_specs": {
                "referer": "https://www.jamovi.org/download.html",
            },
        }

        with patch.dict(
            collect_app_info.cask_cache,
            {api_url: payload},
            clear=True,
        ):
            app_info = collect_app_info.get_homebrew_app_info(api_url)

        self.assertEqual(app_info["url"], source_url)
        self.assertEqual(app_info["vendor_url"], source_url)
        self.assertEqual(app_info["source_sha256"], payload["sha256"])
        self.assertEqual(
            app_info["download_referer"],
            payload["url_specs"]["referer"],
        )

    def test_query_bearing_dmg_is_queued_for_repackaging(self):
        url = "https://formulae.brew.sh/api/cask/raycast.json"
        payload = {
            "name": ["Raycast"],
            "desc": "Launcher",
            "version": "1.0",
            "url": "https://example.test/download?build=arm",
            "sha256": "c" * 64,
            "homepage": "https://example.test/",
            "artifacts": [{"app": ["Raycast.app"]}],
        }
        with patch.dict(collect_app_info.cask_cache, {url: payload}, clear=True):
            app_info = collect_app_info.get_homebrew_app_info(url)
        self.assertEqual(app_info["artifact_kind"], "dmg")
        self.assertEqual(app_info["type"], "app")

    def test_existing_manifest_receives_fresh_routing_metadata(self):
        existing = {
            "artifact_app": "Old.app",
            "artifact_pkg": "Old.pkg",
            "artifact_kind": "archive",
            "archive_format": "tar.gz",
            "download_referer": "https://stale.example.test/download",
        }
        fresh = {
            "artifact_app": "Postman.app",
            "artifact_kind": "archive",
            "archive_format": "zip",
            "source_version": "1.0,101",
            "source_sha256": "d" * 64,
        }

        collect_app_info.sync_artifact_metadata(existing, fresh)

        self.assertEqual(existing["artifact_app"], "Postman.app")
        self.assertEqual(existing["artifact_kind"], "archive")
        self.assertEqual(existing["archive_format"], "zip")
        self.assertEqual(existing["source_version"], "1.0,101")
        self.assertEqual(existing["source_sha256"], "d" * 64)
        self.assertNotIn("artifact_pkg", existing)
        self.assertNotIn("download_referer", existing)

    def test_existing_manifest_drops_stale_archive_format(self):
        existing = {"artifact_kind": "archive", "archive_format": "zip"}

        collect_app_info.sync_artifact_metadata(
            existing,
            {"artifact_kind": "dmg"},
        )

        self.assertEqual(existing["artifact_kind"], "dmg")
        self.assertNotIn("archive_format", existing)

    def test_every_existing_merge_excludes_stale_artifact_metadata(self):
        source = (
            ROOT / ".github/scripts/collect_app_info.py"
        ).read_text(encoding="utf-8")
        merge_section = source.split("# Process regular Homebrew cask URLs", 1)[1]
        merge_section = merge_section.split("# Run custom scrapers", 1)[0]
        self.assertEqual(
            merge_section.count("and key not in ARTIFACT_METADATA_KEYS"),
            4,
        )
        self.assertGreaterEqual(merge_section.count('"bundleId"'), 4)
        self.assertEqual(merge_section.count('"bundleId_source"'), 4)

    def test_installer_only_cask_is_rejected(self):
        url = "https://formulae.brew.sh/api/cask/battle-net.json"
        payload = {
            "name": ["Battle.net"],
            "desc": "Game launcher",
            "version": "1.0",
            "url": "https://example.test/installer",
            "homepage": "https://example.test/",
            "artifacts": [{"installer": [{"manual": "Battle.net-Setup.app"}]}],
        }

        with patch.dict(collect_app_info.cask_cache, {url: payload}, clear=True):
            with self.assertRaises(collect_app_info.CaskUnavailableError) as context:
                collect_app_info.get_homebrew_app_info(url, needs_packaging=True)

        self.assertEqual(
            context.exception.reason,
            collect_app_info.INSTALLER_ONLY_DEPRECATION_REASON,
        )


CASK_INFO = {
    "https://formulae.brew.sh/api/cask/tailscale.json": {
        "name": "Tailscale",
        "description": "Mesh VPN",
        "version": "1.80.0",
        "url": "https://example.com/tailscale-1.80.0.dmg",
        "vendor_url": "https://example.com/tailscale-1.80.0.dmg",
        "bundleId": "io.tailscale.ipn.macos",
        "homepage": "https://tailscale.com/",
        "homebrew_cask": "tailscale",
        "fileName": "tailscale-1.80.0.dmg",
    },
    "https://formulae.brew.sh/api/cask/tailscale-app.json": {
        "name": "Tailscale",
        "description": "Mesh VPN",
        "version": "1.82.0",
        "url": "https://example.com/tailscale-app-1.82.0.dmg",
        "vendor_url": "https://example.com/tailscale-app-1.82.0.dmg",
        "bundleId": "io.tailscale.ipn.macsys",
        "homepage": "https://tailscale.com/",
        "homebrew_cask": "tailscale-app",
        "fileName": "tailscale-app-1.82.0.dmg",
    },
}


def fake_get_homebrew_app_info(json_url, **kwargs):
    return dict(CASK_INFO[json_url])


@contextlib.contextmanager
def run_collector(work_dir, cask_urls, session=None):
    """Run main() against a scratch Apps folder with no network and no README writes.

    Without a session the prefetch is stubbed out and app info comes from CASK_INFO.
    With one, the real prefetch and the real get_homebrew_app_info run against it.
    """
    previous_dir = os.getcwd()
    os.chdir(work_dir)
    output = io.StringIO()
    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(collect_app_info, "app_urls", []))
            stack.enter_context(patch.object(collect_app_info, "homebrew_cask_urls", cask_urls))
            stack.enter_context(patch.object(collect_app_info, "pkg_in_pkg_urls", []))
            stack.enter_context(patch.object(collect_app_info, "pkg_urls", []))
            stack.enter_context(patch.object(collect_app_info, "pkg_in_dmg_urls", []))
            stack.enter_context(patch.object(collect_app_info, "custom_scrapers", []))
            stack.enter_context(patch.dict(collect_app_info.cask_cache, {}, clear=True))
            if session is None:
                stack.enter_context(patch.object(collect_app_info, "prefetch_cask_data", Mock()))
                stack.enter_context(
                    patch.object(
                        collect_app_info, "get_homebrew_app_info", fake_get_homebrew_app_info
                    )
                )
            else:
                stack.enter_context(patch.object(collect_app_info, "cask_session", session))
            stack.enter_context(
                patch.object(collect_app_info, "calculate_file_hash", Mock(return_value="0" * 64))
            )
            stack.enter_context(patch.object(collect_app_info, "update_readme_apps", Mock()))
            stack.enter_context(
                patch.object(collect_app_info, "update_readme_with_latest_changes", Mock())
            )
            stack.enter_context(contextlib.redirect_stdout(output))
            yield output
    finally:
        os.chdir(previous_dir)


class FilenameCollisionTests(unittest.TestCase):
    def setUp(self):
        del collect_app_info.filename_collisions[:]

    def tearDown(self):
        del collect_app_info.filename_collisions[:]

    def write_app(self, directory, name, data):
        path = Path(directory) / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_missing_file_is_claimable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "tailscale.json")

            self.assertTrue(collect_app_info.claim_app_file(path, "tailscale"))
            self.assertEqual(collect_app_info.filename_collisions, [])

    def test_matching_cask_is_claimable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_app(
                directory, "tailscale.json", {"name": "Tailscale", "homebrew_cask": "tailscale"}
            )

            self.assertTrue(collect_app_info.claim_app_file(str(path), "tailscale"))
            self.assertEqual(collect_app_info.filename_collisions, [])

    def test_absent_or_empty_cask_is_claimable(self):
        with tempfile.TemporaryDirectory() as directory:
            missing_key = self.write_app(directory, "no_key.json", {"name": "Tailscale"})
            empty_value = self.write_app(
                directory, "empty.json", {"name": "Tailscale", "homebrew_cask": ""}
            )

            self.assertTrue(collect_app_info.claim_app_file(str(missing_key), "tailscale"))
            self.assertTrue(collect_app_info.claim_app_file(str(empty_value), "tailscale"))
            self.assertEqual(collect_app_info.filename_collisions, [])

    def test_foreign_cask_is_refused_and_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_app(
                directory, "tailscale.json", {"name": "Tailscale", "homebrew_cask": "tailscale"}
            )

            self.assertFalse(collect_app_info.claim_app_file(str(path), "tailscale-app"))
            self.assertEqual(
                collect_app_info.filename_collisions,
                [
                    {
                        "file_path": str(path),
                        "existing_cask": "tailscale",
                        "incoming_cask": "tailscale-app",
                    }
                ],
            )

    def test_deprecated_target_is_guarded_like_any_other_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_app(
                directory,
                "tailscale.json",
                {"name": "Tailscale", "homebrew_cask": "tailscale", "deprecated": True},
            )

            self.assertFalse(collect_app_info.claim_app_file(str(path), "tailscale-app"))
            self.assertEqual(len(collect_app_info.filename_collisions), 1)

    def test_mark_app_deprecated_refuses_foreign_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_app(
                directory, "tailscale.json", {"name": "Tailscale", "homebrew_cask": "tailscale"}
            )
            before = path.read_bytes()

            changed = collect_app_info.mark_app_deprecated(
                directory,
                display_name="Tailscale",
                reason="cask removed from Homebrew",
                cask_token="tailscale-app",
            )

            self.assertFalse(changed)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(len(collect_app_info.filename_collisions), 1)

    def test_second_cask_does_not_overwrite_first_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            os.makedirs(os.path.join(directory, "Apps"))
            app_path = Path(directory) / "Apps" / "tailscale.json"

            with run_collector(
                directory,
                [
                    "https://formulae.brew.sh/api/cask/tailscale.json",
                    "https://formulae.brew.sh/api/cask/tailscale-app.json",
                ],
            ) as output:
                with self.assertRaises(SystemExit) as context:
                    collect_app_info.main()

            self.assertEqual(context.exception.code, 1)
            app_data = json.loads(app_path.read_text(encoding="utf-8"))
            self.assertEqual(app_data["homebrew_cask"], "tailscale")
            self.assertEqual(app_data["version"], "1.80.0")
            self.assertEqual(
                collect_app_info.filename_collisions,
                [
                    {
                        "file_path": os.path.join("Apps", "tailscale.json"),
                        "existing_cask": "tailscale",
                        "incoming_cask": "tailscale-app",
                    }
                ],
            )
            self.assertIn("FILENAME COLLISIONS DETECTED: 1", output.getvalue())

    def test_refused_write_leaves_file_byte_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            os.makedirs(os.path.join(directory, "Apps"))
            app_path = Path(directory) / "Apps" / "tailscale.json"

            with run_collector(directory, ["https://formulae.brew.sh/api/cask/tailscale.json"]):
                collect_app_info.main()

            first_write = app_path.read_bytes()
            self.assertEqual(collect_app_info.filename_collisions, [])

            with run_collector(directory, ["https://formulae.brew.sh/api/cask/tailscale-app.json"]):
                with self.assertRaises(SystemExit) as context:
                    collect_app_info.main()

            self.assertEqual(context.exception.code, 1)
            self.assertEqual(app_path.read_bytes(), first_write)
            self.assertEqual(len(collect_app_info.filename_collisions), 1)

    def test_owning_cask_still_updates_its_file(self):
        with tempfile.TemporaryDirectory() as directory:
            os.makedirs(os.path.join(directory, "Apps"))
            app_path = Path(directory) / "Apps" / "tailscale.json"
            url = "https://formulae.brew.sh/api/cask/tailscale.json"

            with run_collector(directory, [url]):
                collect_app_info.main()

            bumped = dict(CASK_INFO[url], version="1.81.0")
            with patch.dict(CASK_INFO, {url: bumped}):
                with run_collector(directory, [url]):
                    collect_app_info.main()

            app_data = json.loads(app_path.read_text(encoding="utf-8"))
            self.assertEqual(app_data["version"], "1.81.0")
            self.assertEqual(app_data["previous_version"], "1.80.0")
            self.assertEqual(app_data["homebrew_cask"], "tailscale")
            self.assertEqual(collect_app_info.filename_collisions, [])

    def test_file_without_stored_cask_is_updated_normally(self):
        with tempfile.TemporaryDirectory() as directory:
            os.makedirs(os.path.join(directory, "Apps"))
            app_path = self.write_app(
                os.path.join(directory, "Apps"),
                "tailscale.json",
                {"name": "Tailscale", "version": "1.0.0", "homebrew_cask": ""},
            )

            with run_collector(directory, ["https://formulae.brew.sh/api/cask/tailscale-app.json"]):
                collect_app_info.main()

            app_data = json.loads(app_path.read_text(encoding="utf-8"))
            self.assertEqual(app_data["homebrew_cask"], "tailscale-app")
            self.assertEqual(app_data["version"], "1.82.0")
            self.assertEqual(collect_app_info.filename_collisions, [])


TAILSCALE_PAYLOAD = {
    "name": ["Tailscale"],
    "desc": "Mesh VPN",
    "version": "1.80.0",
    "url": "https://example.com/tailscale-1.80.0.dmg",
    "homepage": "https://tailscale.com/",
    "artifacts": [
        {"app": ["Tailscale.app"]},
        {"uninstall": [{"quit": "io.tailscale.ipn.macos"}]},
    ],
}


class PrefetchTests(unittest.TestCase):
    def setUp(self):
        collect_app_info.cask_cache.clear()
        del collect_app_info.filename_collisions[:]

    def tearDown(self):
        collect_app_info.cask_cache.clear()
        del collect_app_info.filename_collisions[:]

    def test_prefetch_stores_payload_or_exception_per_url(self):
        good_url = "https://formulae.brew.sh/api/cask/tailscale.json"
        missing_url = "https://formulae.brew.sh/api/cask/removed-app.json"
        session = CountingSession(
            {
                good_url: cask_response(TAILSCALE_PAYLOAD),
                missing_url: cask_response(status_code=404),
            }
        )

        with patch.object(collect_app_info, "cask_session", session):
            with contextlib.redirect_stdout(io.StringIO()):
                cache = collect_app_info.prefetch_cask_data([good_url, missing_url])

        self.assertEqual(cache[good_url], TAILSCALE_PAYLOAD)
        self.assertIsInstance(cache[missing_url], collect_app_info.CaskUnavailableError)
        self.assertEqual(cache[missing_url].cask_token, "removed-app")
        self.assertEqual(sorted(session.requested), sorted([good_url, missing_url]))

    def test_prefetched_payload_is_consumed_without_a_second_fetch(self):
        url = "https://formulae.brew.sh/api/cask/tailscale.json"
        session = CountingSession({url: cask_response(TAILSCALE_PAYLOAD)})

        with patch.object(collect_app_info, "cask_session", session):
            with contextlib.redirect_stdout(io.StringIO()):
                collect_app_info.prefetch_cask_data([url])
            app_info = collect_app_info.get_homebrew_app_info(url)

        self.assertEqual(app_info["name"], "Tailscale")
        self.assertEqual(app_info["version"], "1.80.0")
        self.assertEqual(app_info["homebrew_cask"], "tailscale")
        self.assertEqual(session.requested, [url])

    def test_cache_miss_falls_back_to_a_session_fetch(self):
        url = "https://formulae.brew.sh/api/cask/tailscale.json"
        session = CountingSession({url: cask_response(TAILSCALE_PAYLOAD)})

        with patch.object(collect_app_info, "cask_session", session):
            app_info = collect_app_info.get_homebrew_app_info(url)

        self.assertEqual(session.requested, [url])
        self.assertEqual(app_info["version"], "1.80.0")

    def test_prefetched_404_still_deprecates_through_the_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            apps_folder = os.path.join(directory, "Apps")
            os.makedirs(apps_folder)
            app_path = Path(apps_folder) / "vmware_fusion.json"
            app_path.write_text(
                json.dumps(
                    {
                        "name": "VMware Fusion",
                        "version": "13.6.3",
                        "homebrew_cask": "vmware-fusion",
                    }
                ),
                encoding="utf-8",
            )
            url = "https://formulae.brew.sh/api/cask/vmware-fusion.json"
            session = CountingSession({url: cask_response(status_code=404)})

            with run_collector(directory, [url], session=session):
                collect_app_info.main()

            app_data = json.loads(app_path.read_text(encoding="utf-8"))
            self.assertTrue(app_data["deprecated"])
            self.assertEqual(app_data["deprecation_reason"], "cask removed from Homebrew")
            self.assertEqual(session.requested, [url])

    def test_url_in_two_lists_is_fetched_once(self):
        with tempfile.TemporaryDirectory() as directory:
            os.makedirs(os.path.join(directory, "Apps"))
            url = "https://formulae.brew.sh/api/cask/tailscale.json"
            session = CountingSession({url: cask_response(TAILSCALE_PAYLOAD)})

            with run_collector(directory, [url], session=session):
                with patch.object(collect_app_info, "pkg_urls", [url]):
                    collect_app_info.main()

            self.assertEqual(session.requested, [url])
            app_data = json.loads(
                (Path(directory) / "Apps" / "tailscale.json").read_text(encoding="utf-8")
            )
            # The later list owns the type, which only holds if both loops saw the cask.
            self.assertEqual(app_data["type"], "pkg")
            self.assertEqual(collect_app_info.filename_collisions, [])

    def test_all_source_mismatches_are_reported_after_remaining_apps_run(self):
        urls = [
            "https://formulae.brew.sh/api/cask/tailscale.json",
            "https://formulae.brew.sh/api/cask/tailscale-app.json",
        ]

        def mismatch(app_info):
            raise collect_app_info.SourceHashMismatchError(
                app_info["name"],
                "a" * 64,
                "b" * 64,
                app_info["url"],
                app_info["homebrew_cask"],
            )

        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            os.makedirs(os.path.join(directory, "Apps"))
            with (
                run_collector(directory, urls),
                patch.object(
                    collect_app_info,
                    "calculate_verified_source_hash",
                    side_effect=mismatch,
                ) as verify,
                patch.object(
                    collect_app_info,
                    "restore_mismatched_manifest",
                ) as restore,
                contextlib.redirect_stderr(stderr),
                self.assertRaisesRegex(
                    RuntimeError,
                    "verification failed for 2 app",
                ),
            ):
                collect_app_info.main()

        self.assertEqual(verify.call_count, 2)
        self.assertEqual(restore.call_count, 2)
        summary = stderr.getvalue()
        self.assertIn('"cask": "tailscale"', summary)
        self.assertIn('"cask": "tailscale-app"', summary)
        self.assertIn('"expected_sha256": "' + "a" * 64 + '"', summary)
        self.assertIn('"actual_sha256": "' + "b" * 64 + '"', summary)

    def test_mismatch_restore_replaces_partial_manifest_with_head_bytes(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            apps_folder = Path(directory) / "Apps"
            apps_folder.mkdir()
            manifest = apps_folder / "example.json"
            manifest.write_text(
                json.dumps(
                    {
                        "name": "Example",
                        "homebrew_cask": "example",
                        "version": "partial",
                    }
                ),
                encoding="utf-8",
            )
            original = b'{"name":"Example","version":"stable"}\n'
            mismatch = collect_app_info.SourceHashMismatchError(
                "Example",
                "a" * 64,
                "b" * 64,
                "https://example.test/app.dmg",
                "example",
            )
            completed = Mock(returncode=0, stdout=original)

            with patch.object(
                collect_app_info.subprocess,
                "run",
                return_value=completed,
            ) as run:
                collect_app_info.restore_mismatched_manifest(
                    apps_folder, mismatch
                )

            self.assertEqual(manifest.read_bytes(), original)
            self.assertIn("HEAD:", run.call_args.args[0][2])


def download_response(body=b"", status_code=200):
    """Build a fake streaming response for one calculate_file_hash attempt."""
    response = Mock(status_code=status_code)
    if status_code >= 400:
        response.raise_for_status.side_effect = collect_app_info.requests.HTTPError(
            f"{status_code} Client Error", response=response
        )
    else:
        response.raise_for_status.return_value = None
    response.iter_content.return_value = iter([body] if body else [])
    return response


class CalculateFileHashTests(unittest.TestCase):
    """The agent cascade mirrors ATTEMPTS in check_download_urls.py."""

    def test_agents_stay_in_sync_with_the_url_health_checker(self):
        spec = importlib.util.spec_from_file_location(
            "check_download_urls", ROOT / ".github/scripts/check_download_urls.py"
        )
        health_check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(health_check)

        sibling_agents = []
        for attempt in health_check.ATTEMPTS:
            if attempt["user_agent"] not in sibling_agents:
                sibling_agents.append(attempt["user_agent"])

        self.assertEqual(list(collect_app_info.DOWNLOAD_USER_AGENTS), sibling_agents)

    def test_request_carries_a_user_agent_header(self):
        payload = b"payload bytes"
        get = Mock(return_value=download_response(payload))

        with patch.object(collect_app_info.requests, "get", get):
            digest = collect_app_info.calculate_file_hash("https://example.test/app.dmg")

        self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
        get.assert_called_once()
        headers = get.call_args.kwargs["headers"]
        self.assertEqual(
            headers["User-Agent"], collect_app_info.DOWNLOAD_USER_AGENTS[0]
        )
        self.assertNotIn("Referer", headers)

    def test_jamovi_referer_reaches_redirected_hash_download(self):
        payload = b"koly" + b"\x00" * 64
        referer = "https://www.jamovi.org/download.html"

        def get(_url, **kwargs):
            if kwargs["headers"].get("Referer") != referer:
                return download_response(b"<html>not found</html>")
            response = download_response(payload)
            response.url = "https://dl-cdn.jamovi.org/releases/jamovi.dmg"
            return response

        request = Mock(side_effect=get)
        with patch.object(collect_app_info.requests, "get", request):
            digest = collect_app_info.calculate_file_hash(
                "https://www.jamovi.org/downloads/jamovi.dmg",
                referer=referer,
            )

        self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
        self.assertEqual(
            request.call_args.kwargs["headers"]["Referer"],
            referer,
        )

    def test_verified_hash_passes_only_persisted_referer(self):
        app_info = {
            "name": "jamovi",
            "url": "https://www.jamovi.org/downloads/jamovi.dmg",
            "download_referer": "https://www.jamovi.org/download.html",
            "source_sha256": "a" * 64,
        }
        with patch.object(
            collect_app_info,
            "calculate_file_hash",
            return_value="a" * 64,
        ) as calculate:
            collect_app_info.calculate_verified_source_hash(app_info)

        calculate.assert_called_once_with(
            app_info["url"],
            referer=app_info["download_referer"],
            user_agent_mode="default",
        )

    def test_warp_hash_uses_fixed_homebrew_user_agent(self):
        payload = b"koly" + b"\x00" * 64
        request = Mock(return_value=download_response(payload))

        with patch.object(collect_app_info.requests, "get", request):
            digest = collect_app_info.calculate_file_hash(
                "https://app.warp.dev/download/brew?version=v1",
                user_agent_mode="homebrew",
            )

        self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
        self.assertEqual(
            request.call_args.kwargs["headers"]["User-Agent"],
            collect_app_info.HOMEBREW_DOWNLOAD_USER_AGENT,
        )
        request.assert_called_once()

    def test_rejected_agent_falls_through_to_the_next_one(self):
        payload = b"the real installer"
        responses = [
            download_response(status_code=403),
            download_response(payload),
        ]
        get = Mock(side_effect=responses)

        with patch.object(collect_app_info.requests, "get", get):
            digest = collect_app_info.calculate_file_hash("https://example.test/app.dmg")

        # The rejection is retried, not swallowed, and the digest is the real file's.
        self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
        self.assertEqual(get.call_count, 2)
        used_agents = [call.kwargs["headers"]["User-Agent"] for call in get.call_args_list]
        self.assertEqual(used_agents, list(collect_app_info.DOWNLOAD_USER_AGENTS[:2]))

    def test_every_agent_rejected_yields_no_hash(self):
        get = Mock(side_effect=lambda *a, **kw: download_response(status_code=403))

        with patch.object(collect_app_info.requests, "get", get):
            digest = collect_app_info.calculate_file_hash("https://example.test/app.dmg")

        self.assertIsNone(digest)
        self.assertEqual(get.call_count, len(collect_app_info.DOWNLOAD_USER_AGENTS))

    def test_html_from_one_agent_falls_through_but_never_gets_hashed(self):
        payload = b"the real installer"
        get = Mock(
            side_effect=[
                download_response(b"<!DOCTYPE html><html>blocked</html>"),
                download_response(payload),
            ]
        )

        with patch.object(collect_app_info.requests, "get", get):
            digest = collect_app_info.calculate_file_hash("https://example.test/app.dmg")

        self.assertEqual(digest, hashlib.sha256(payload).hexdigest())

    def test_html_from_every_agent_is_refused(self):
        get = Mock(
            side_effect=lambda *a, **kw: download_response(b"<html>blocked</html>")
        )

        with patch.object(collect_app_info.requests, "get", get):
            digest = collect_app_info.calculate_file_hash("https://example.test/app.dmg")

        self.assertIsNone(digest)

    def test_empty_body_is_refused_after_every_agent(self):
        get = Mock(side_effect=lambda *a, **kw: download_response(b""))

        with patch.object(collect_app_info.requests, "get", get):
            digest = collect_app_info.calculate_file_hash("https://example.test/app.dmg")

        self.assertIsNone(digest)
        self.assertEqual(get.call_count, len(collect_app_info.DOWNLOAD_USER_AGENTS))

    def test_oversized_body_aborts_without_trying_more_agents(self):
        get = Mock(side_effect=lambda *a, **kw: download_response(b"x" * 64))

        with patch.object(collect_app_info, "MAX_DOWNLOAD_BYTES", 8):
            with patch.object(collect_app_info.requests, "get", get):
                digest = collect_app_info.calculate_file_hash(
                    "https://example.test/app.dmg"
                )

        self.assertIsNone(digest)
        self.assertEqual(get.call_count, 1)


class CatalogConsistencyTests(unittest.TestCase):
    def test_visual_paradigm_quarantine_evidence_and_tombstone_are_consistent(self):
        quarantine = json.loads(
            (
                ROOT / ".github/data/source-integrity-quarantine.json"
            ).read_text(encoding="utf-8")
        )["visual-paradigm"]
        manifest = json.loads(
            (ROOT / "Apps/visual_paradigm.json").read_text(encoding="utf-8")
        )
        supported = json.loads(
            (ROOT / "supported_apps.json").read_text(encoding="utf-8")
        )

        self.assertEqual(
            quarantine["expected_sha256"],
            "725c3c81d254d32c7a9f920d23d14a7694be30c52c99d28d09c457f2a24ddd24",
        )
        self.assertEqual(
            quarantine["actual_sha256"],
            "fc3c40f94cf88841170e2fad78f85f3c3465ff6b5f502aa60c23c0262708f19b",
        )
        self.assertEqual(quarantine["url"], manifest["url"])
        self.assertTrue(manifest["deprecated"])
        self.assertEqual(
            manifest["deprecation_reason"],
            quarantine["reason"],
        )
        self.assertNotIn("visual_paradigm", supported)

    def test_unowned_catalog_entries_have_deterministic_disposition(self):
        configured = (
            collect_app_info.app_urls
            + collect_app_info.homebrew_cask_urls
            + collect_app_info.pkg_in_pkg_urls
            + collect_app_info.pkg_urls
            + collect_app_info.pkg_in_dmg_urls
        )
        self.assertIn(
            "https://formulae.brew.sh/api/cask/linear.json",
            configured,
        )
        self.assertIn(
            "https://formulae.brew.sh/api/cask/rhino-app.json",
            configured,
        )
        self.assertNotIn(
            "https://formulae.brew.sh/api/cask/abstract.json",
            configured,
        )
        self.assertNotIn(
            "https://formulae.brew.sh/api/cask/ubar.json",
            configured,
        )
        for filename in ("graphiql_app.json", "abstract.json", "ubar.json"):
            with self.subTest(filename=filename):
                app = json.loads(
                    (ROOT / "Apps" / filename).read_text(encoding="utf-8")
                )
                self.assertTrue(app["deprecated"])
                self.assertTrue(app["deprecation_reason"])

    def test_formula_api_urls_are_not_configured_as_apps(self):
        configured = (
            collect_app_info.app_urls
            + collect_app_info.homebrew_cask_urls
            + collect_app_info.pkg_in_pkg_urls
            + collect_app_info.pkg_urls
            + collect_app_info.pkg_in_dmg_urls
        )
        self.assertFalse(any("/api/formula/" in url for url in configured))

    def test_dmg_with_declared_pkg_routes_to_pkg_in_dmg(self):
        url = "https://formulae.brew.sh/api/cask/example-driver.json"
        payload = {
            "name": ["Example Driver"],
            "desc": "Driver",
            "version": "1",
            "url": "https://example.test/driver.dmg",
            "sha256": "a" * 64,
            "homepage": "https://example.test/",
            "artifacts": [{"pkg": ["Driver.pkg"]}],
        }
        with patch.dict(collect_app_info.cask_cache, {url: payload}, clear=True):
            info = collect_app_info.get_homebrew_app_info(url)
        self.assertEqual(info["type"], "pkg_in_dmg")

    def test_codex_uses_desktop_cask_instead_of_cli_cask(self):
        desktop_url = "https://formulae.brew.sh/api/cask/codex-app.json"
        cli_url = "https://formulae.brew.sh/api/cask/codex.json"

        self.assertIn(desktop_url, collect_app_info.app_urls)
        self.assertNotIn(cli_url, collect_app_info.app_urls)

        codex = json.loads(
            (ROOT / "Apps" / "codex.json").read_text(encoding="utf-8")
        )
        self.assertEqual(codex["homebrew_cask"], "codex-app")

    def test_binary_only_incident_casks_are_not_configured(self):
        configured_urls = (
            collect_app_info.app_urls
            + collect_app_info.homebrew_cask_urls
            + collect_app_info.pkg_in_pkg_urls
            + collect_app_info.pkg_urls
            + collect_app_info.pkg_in_dmg_urls
        )

        self.assertNotIn(
            "https://formulae.brew.sh/api/cask/codex.json",
            configured_urls,
        )
        self.assertNotIn(
            "https://formulae.brew.sh/api/cask/copilot-cli.json",
            configured_urls,
        )
        for token in (
            "1password-cli",
            "android-commandlinetools",
            "android-platform-tools",
            "autodesk-fusion",
            "expressvpn",
            "sentinel",
        ):
            with self.subTest(token=token):
                self.assertNotIn(
                    f"https://formulae.brew.sh/api/cask/{token}.json",
                    configured_urls,
                )

    def test_installer_only_incident_casks_are_deprecated(self):
        expected_files = {
            "battle-net": "blizzard_battlenet.json",
            "blockblock": "blockblock.json",
            "boinc": "berkeley_open_infrastructure_for_network_computing.json",
            "logi-options+": "logitech_options.json",
            "logitech-g-hub": "logitech_g_hub.json",
            "oversight": "oversight.json",
            "private-internet-access": "private_internet_access.json",
        }
        supported = json.loads(
            (ROOT / "supported_apps.json").read_text(encoding="utf-8")
        )

        for token, filename in expected_files.items():
            with self.subTest(token=token):
                app = json.loads(
                    (ROOT / "Apps" / filename).read_text(encoding="utf-8")
                )
                self.assertEqual(app["homebrew_cask"], token)
                self.assertTrue(app["deprecated"])
                self.assertEqual(
                    app["deprecation_reason"],
                    collect_app_info.INSTALLER_ONLY_DEPRECATION_REASON,
                )
                self.assertNotIn(Path(filename).stem, supported)

    def test_mitmproxy_binary_tombstone_is_preserved(self):
        app = json.loads(
            (ROOT / "Apps/mitmproxy.json").read_text(encoding="utf-8")
        )
        supported = json.loads(
            (ROOT / "supported_apps.json").read_text(encoding="utf-8")
        )
        self.assertTrue(app["deprecated"])
        self.assertNotIn("mitmproxy", supported)

    def test_supported_catalog_matches_valid_apps(self):
        apps = {
            path.stem: json.loads(path.read_text(encoding="utf-8"))
            for path in (ROOT / "Apps").glob("*.json")
        }
        supported = json.loads(
            (ROOT / "supported_apps.json").read_text(encoding="utf-8")
        )
        generator_spec = importlib.util.spec_from_file_location(
            "generate_supported_apps",
            ROOT / ".github/scripts/generate_supported_apps.py",
        )
        generator = importlib.util.module_from_spec(generator_spec)
        generator_spec.loader.exec_module(generator)
        expected = {
            name
            for name, app_data in apps.items()
            if generator.is_publishable(app_data)
        }

        self.assertEqual(set(supported), expected)

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        badge = re.search(r"Apps_Available-(\d+)-", readme)
        self.assertIsNotNone(badge)
        self.assertEqual(int(badge.group(1)), len(expected))


if __name__ == "__main__":
    unittest.main()
