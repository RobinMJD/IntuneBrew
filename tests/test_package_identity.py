import importlib.util
import plistlib
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "select_package_identity",
    ROOT / ".github/scripts/select_package_identity.py",
)
identity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(identity)


def write_app(root, path, bundle_id):
    plist = root / path / "Contents/Info.plist"
    plist.parent.mkdir(parents=True)
    with plist.open("wb") as handle:
        plistlib.dump({"CFBundleIdentifier": bundle_id}, handle)


def write_package_info(root, path, package_id):
    info = root / path / "PackageInfo"
    info.parent.mkdir(parents=True)
    info.write_text(
        f'<pkg-info identifier="{package_id}" version="1"/>',
        encoding="utf-8",
    )


class PackageIdentityTests(unittest.TestCase):
    def test_cisco_jabber_expected_app_beats_plugin_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_app(root, Path("Applications/Cisco Jabber.app"), "com.cisco.Jabber")
            write_package_info(
                root,
                Path("Plugins/JabberPlugin.pkg"),
                "com.cisco.jabber.plugin",
            )
            apps = identity.app_identifiers(root)
            self.assertEqual(identity.choose(apps, "com.cisco.Jabber"), "com.cisco.Jabber")

    def test_aspera_app_beats_crypt_component(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_app(root, Path("Aspera Connect.app"), "com.ibm.aspera.connect")
            write_package_info(
                root,
                Path("CryptHelper.pkg"),
                "com.ibm.aspera.crypt",
            )
            self.assertEqual(
                identity.choose(
                    identity.app_identifiers(root),
                    "com.ibm.aspera.connect",
                ),
                "com.ibm.aspera.connect",
            )

    def test_spyder_app_beats_shortcuts_component(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_app(root, Path("Spyder.app"), "org.spyder-ide")
            write_package_info(
                root,
                Path("Shortcuts.pkg"),
                "org.spyder.shortcuts",
            )
            self.assertEqual(
                identity.choose(identity.app_identifiers(root), "org.spyder-ide"),
                "org.spyder-ide",
            )

    def test_tailscale_package_info_used_when_no_primary_app_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_app(
                root,
                Path("Frameworks/Sparkle.framework/Updater.app"),
                "org.sparkle-project.Updater",
            )
            write_package_info(
                root,
                Path("Tailscale.pkg"),
                "com.tailscale.ipn.macsys",
            )
            self.assertEqual(identity.app_identifiers(root), [])
            self.assertEqual(
                identity.select_identity(
                    root,
                    "com.tailscale.ipn.macsys",
                ),
                "com.tailscale.ipn.macsys",
            )

    def test_multiple_primary_apps_without_expected_match_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_app(root, Path("One.app"), "com.example.one")
            write_app(root, Path("Two.app"), "com.example.two")
            self.assertIsNone(identity.choose(identity.app_identifiers(root), ""))

    def test_cryptomator_and_veracrypt_are_not_false_helpers(self):
        self.assertFalse(
            identity.excluded(
                Path("Applications/VeraCrypt.app/Contents/Info.plist")
            )
        )
        self.assertFalse(
            identity.excluded(
                Path("Applications/Cryptomator.app/Contents/Info.plist")
            )
        )
        self.assertTrue(
            identity.excluded(
                Path("Applications/Crypt Helper.app/Contents/Info.plist")
            )
        )


if __name__ == "__main__":
    unittest.main()
