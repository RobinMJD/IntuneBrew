import argparse
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parents[2]
APPS_DIR = ROOT / "Apps"
SUPPORTED_PATH = ROOT / "supported_apps.json"
README_PATH = ROOT / "README.md"
REQUIRED_FIELDS = ("name", "version", "bundleId", "url", "fileName", "sha")
UNSUPPORTED_CASKS = {
    "1password-cli",
    "android-commandlinetools",
    "android-platform-tools",
    "autodesk-fusion",
    "expressvpn",
    "sentinel",
}
LEGACY_PACKAGE_HOSTS = {"intunebrew.blob.core.windows.net"}


def publication_errors(app):
    errors = []
    if app.get("homebrew_cask") in UNSUPPORTED_CASKS:
        errors.append("unsupported cask")
    for field in REQUIRED_FIELDS:
        if not app.get(field):
            errors.append(f"missing {field}")
    bundle_id = app.get("bundleId")
    if (
        not isinstance(bundle_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", bundle_id)
        or any(char in bundle_id for char in "*?[]")
    ):
        errors.append("invalid bundleId")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", str(app.get("sha", ""))):
        errors.append("invalid sha")
    if (
        app.get("type") in {"app", "pkg_in_dmg", "pkg_in_pkg"}
        and app.get("source_sha256") == "no_check"
        and not re.fullmatch(
            r"[0-9a-fA-F]{64}",
            str(app.get("source_observed_sha256", "")),
        )
    ):
        errors.append("missing observed source digest")

    filename = unquote(str(app.get("fileName", "")))
    if "/" in filename or "\\" in filename:
        errors.append("unsafe filename")
    if not filename.lower().endswith((".pkg", ".dmg")):
        errors.append("non-deployable filename")

    parsed_url = urlparse(str(app.get("url", "")))
    if parsed_url.scheme != "https":
        errors.append("non-HTTPS URL")
    if parsed_url.username or parsed_url.password:
        errors.append("URL contains userinfo")
    try:
        if parsed_url.port not in (None, 443):
            errors.append("non-default URL port")
    except ValueError:
        errors.append("invalid URL port")
    if parsed_url.query:
        errors.append("URL contains query")
    if parsed_url.fragment:
        errors.append("URL contains fragment")
    if parsed_url.hostname in LEGACY_PACKAGE_HOSTS and parsed_url.path.lower().endswith(".pkg"):
        errors.append("legacy package URL")
    return errors


def is_publishable(app):
    return not app.get("deprecated") and not publication_errors(app)


def generate_supported_apps(allow_incomplete=False, exclude_invalid=False):
    supported = {}
    invalid = []
    for path in APPS_DIR.glob("*.json"):
        try:
            app = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            invalid.append(f"{path.name}: invalid JSON ({error})")
            continue
        if app.get("deprecated"):
            print(f"Excluding deprecated app: {path.name}")
            continue
        errors = publication_errors(app)
        if not errors or (allow_incomplete and not exclude_invalid):
            supported[path.stem] = (
                "https://raw.githubusercontent.com/ugurkocde/IntuneBrew/main/"
                f"Apps/{path.name}"
            )
        if errors:
            invalid.append(f"{path.name}: {', '.join(errors)}")

    if invalid and not allow_incomplete and not exclude_invalid:
        raise SystemExit(
            "Catalog publication contract failed:\n- " + "\n- ".join(invalid)
        )

    supported = dict(sorted(supported.items()))
    SUPPORTED_PATH.write_text(
        json.dumps(supported, indent=4) + "\n",
        encoding="utf-8",
    )

    readme = README_PATH.read_text(encoding="utf-8")
    readme = re.sub(
        r"(Apps_Available-)\d+(-2ea44f\?style=flat)",
        rf"\g<1>{len(supported)}\g<2>",
        readme,
    )
    README_PATH.write_text(readme, encoding="utf-8")
    print(f"Generated supported_apps.json with {len(supported)} apps")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="Keep non-deprecated packaging candidates before Process apps",
    )
    parser.add_argument(
        "--exclude-invalid",
        action="store_true",
        help="Write a safe progress index containing only valid manifests",
    )
    args = parser.parse_args()
    generate_supported_apps(args.allow_incomplete, args.exclude_invalid)
