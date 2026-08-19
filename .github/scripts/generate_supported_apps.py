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
    if app.get("deprecated"):
        errors.append("deprecated")
    if app.get("homebrew_cask") in UNSUPPORTED_CASKS:
        errors.append("unsupported cask")
    for field in REQUIRED_FIELDS:
        if not app.get(field):
            errors.append(f"missing {field}")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", str(app.get("sha", ""))):
        errors.append("invalid sha")

    filename = unquote(str(app.get("fileName", "")))
    if "/" in filename or "\\" in filename:
        errors.append("unsafe filename")
    if not filename.lower().endswith((".pkg", ".dmg")):
        errors.append("non-deployable filename")

    parsed_url = urlparse(str(app.get("url", "")))
    if parsed_url.hostname in LEGACY_PACKAGE_HOSTS and parsed_url.path.lower().endswith(".pkg"):
        errors.append("legacy package URL")
    return errors


def is_publishable(app):
    return not publication_errors(app)


def generate_supported_apps():
    supported = {}
    for path in APPS_DIR.glob("*.json"):
        try:
            app = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        errors = publication_errors(app)
        if not errors:
            supported[path.stem] = (
                "https://raw.githubusercontent.com/ugurkocde/IntuneBrew/main/"
                f"Apps/{path.name}"
            )
        else:
            print(f"Excluding {path.name}: {', '.join(errors)}")

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
    generate_supported_apps()
