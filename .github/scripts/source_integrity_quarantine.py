import json
import re
from datetime import date
from urllib.parse import urlparse


SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
KNOWN_FAILURE_TYPES = frozenset({"sha_mismatch", "access_denied"})


def _require_nonempty_string(entry, field, token):
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(
            f"Source integrity quarantine entry for {token} has invalid {field}"
        )
    return value


def _require_sha256(entry, field, token):
    value = _require_nonempty_string(entry, field, token)
    if not SHA256_PATTERN.fullmatch(value):
        raise RuntimeError(
            f"Source integrity quarantine entry for {token} has invalid {field}"
        )


def _validate_common_fields(token, entry):
    if not isinstance(entry, dict):
        raise RuntimeError(
            f"Source integrity quarantine entry for {token} must contain an object"
        )
    _require_nonempty_string(entry, "reason", token)
    url = _require_nonempty_string(entry, "url", token)
    if urlparse(url).scheme != "https":
        raise RuntimeError(
            f"Source integrity quarantine entry for {token} has invalid url"
        )
    observed_at = _require_nonempty_string(entry, "observed_at", token)
    try:
        date.fromisoformat(observed_at)
    except ValueError as error:
        raise RuntimeError(
            f"Source integrity quarantine entry for {token} has invalid observed_at"
        ) from error


def _validate_sha_mismatch(token, entry):
    _require_sha256(entry, "expected_sha256", token)
    _require_sha256(entry, "actual_sha256", token)
    workflow_run_id = entry.get("workflow_run_id")
    if type(workflow_run_id) is not int or workflow_run_id <= 0:
        raise RuntimeError(
            f"Source integrity quarantine entry for {token} has invalid workflow_run_id"
        )


def _validate_access_denied(token, entry):
    if entry.get("cask") != token:
        raise RuntimeError(
            f"Source integrity quarantine entry for {token} has invalid cask"
        )
    _require_sha256(entry, "expected_sha256", token)
    if "actual_sha256" in entry:
        raise RuntimeError(
            f"Source integrity quarantine entry for {token} must not claim actual_sha256"
        )
    workflow_runs = entry.get("workflow_runs")
    if not isinstance(workflow_runs, list) or not workflow_runs:
        raise RuntimeError(
            f"Source integrity quarantine entry for {token} has no workflow run evidence"
        )
    for evidence in workflow_runs:
        if not isinstance(evidence, dict):
            raise RuntimeError(
                f"Source integrity quarantine entry for {token} has invalid workflow run evidence"
            )
        run_id = evidence.get("run_id")
        attempt = evidence.get("attempt")
        if (
            type(run_id) is not int
            or run_id <= 0
            or type(attempt) is not int
            or attempt <= 0
        ):
            raise RuntimeError(
                f"Source integrity quarantine entry for {token} has invalid workflow run evidence"
            )


def validate_source_integrity_quarantine(quarantine):
    if not isinstance(quarantine, dict):
        raise RuntimeError("Source integrity quarantine must contain an object")
    for token, entry in quarantine.items():
        if not isinstance(token, str) or not token.strip():
            raise RuntimeError("Source integrity quarantine has an invalid cask token")
        _validate_common_fields(token, entry)
        failure_type = entry.get("failure_type", "sha_mismatch")
        if failure_type not in KNOWN_FAILURE_TYPES:
            raise RuntimeError(
                f"Source integrity quarantine entry for {token} has invalid failure_type"
            )
        if failure_type == "sha_mismatch":
            _validate_sha_mismatch(token, entry)
        else:
            _validate_access_denied(token, entry)
    return quarantine


def load_source_integrity_quarantine(path):
    try:
        with path.open(encoding="utf-8") as handle:
            quarantine = json.load(handle)
    except (OSError, ValueError) as error:
        raise RuntimeError(
            f"Could not load source integrity quarantine: {error}"
        ) from error
    return validate_source_integrity_quarantine(quarantine)


def get_quarantine_reason(quarantine, token):
    entry = quarantine.get(token)
    if entry is None:
        return None
    reason = entry.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise RuntimeError(
            f"Source integrity quarantine entry for {token} has no reason"
        )
    return reason
