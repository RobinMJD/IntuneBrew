import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_PATH = ROOT / "IntuneBrew_Runbook.ps1"
CONTROL_PATHS = {
    "readiness": ROOT / "AzureAutomation/IntuneBrew-Readiness.ps1",
    "audit": ROOT / "AzureAutomation/IntuneBrew-UpdateAudit.ps1",
    "monitor": ROOT / "AzureAutomation/IntuneBrew-UpstreamMonitor.ps1",
}
ALL_PATHS = (PRODUCTION_PATH, *CONTROL_PATHS.values())


class AzureAutomationRunbookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.production = PRODUCTION_PATH.read_text(encoding="utf-8")
        cls.controls = {
            name: path.read_text(encoding="utf-8")
            for name, path in CONTROL_PATHS.items()
        }
        cls.all_scripts = "\n".join(path.read_text(encoding="utf-8") for path in ALL_PATHS)

    def test_production_is_update_only_and_gated(self):
        self.assertIn("$UseExistingIntuneApp -ne $true", self.production)
        self.assertIn("$CopyAssignments -eq $true", self.production)
        self.assertIn("$MaxAppsPerRun -lt 1", self.production)
        self.assertIn("$MaxAppsPerRun -gt 3", self.production)
        self.assertIn("This deployment never creates new Intune apps", self.production)
        self.assertNotRegex(
            self.production,
            re.compile(
                r"Invoke-MgGraphRequest\s+-Method\s+POST\s+-Uri\s+"
                r"[\"']https://graph\.microsoft\.com/beta/"
                r"deviceAppManagement/mobileApps[\"']",
                re.IGNORECASE,
            ),
        )
        self.assertNotIn("New-MgDeviceAppManagementMobileApp", self.production)
        self.assertNotIn("Creating New Intune App", self.production)

    def test_catalog_marker_is_fork_scoped_and_fail_closed(self):
        for path in ALL_PATHS:
            script = path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertIn(
                    "commits?path=.github/catalog-state.json&per_page=1",
                    script,
                )
                self.assertIn(
                    "[string]$state.repository -ne 'RobinMJD/IntuneBrew'",
                    script,
                )
                self.assertIn(
                    "[string]$markerCommitDetails.parents[0].sha -ne "
                    "[string]$state.catalogCommit",
                    script,
                )
                self.assertIn("[string]$run.status -ne 'completed'", script)
                self.assertIn("[string]$run.conclusion -ne 'success'", script)
                self.assertIn("[string]$run.head_branch -ne 'main'", script)
                self.assertIn(
                    "[string]$run.repository.full_name -ne "
                    "[string]$state.repository",
                    script,
                )

    def test_catalog_content_is_commit_addressed(self):
        self.assertIn(
            'supportedAppsUrl = "https://raw.githubusercontent.com/'
            'RobinMJD/IntuneBrew/$catalogCommit/supported_apps.json"',
            self.production,
        )
        self.assertIn(
            "ConvertTo-CommitManifestUri -Uri ([string]$_) -Commit $catalogCommit",
            self.production,
        )
        self.assertIn(
            '$decodedPath -notmatch "^/(?:RobinMJD|ugurkocde)/'
            'IntuneBrew/main/Apps/',
            self.production,
        )
        self.assertIn(
            '$archiveUri = "https://codeload.github.com/'
            'RobinMJD/IntuneBrew/zip/$catalogCommit"',
            self.controls["audit"],
        )
        self.assertIn(
            '$catalogUri = "https://raw.githubusercontent.com/'
            'RobinMJD/IntuneBrew/$catalogCommit/supported_apps.json"',
            self.controls["readiness"],
        )
        self.assertNotIn("(?:refs/heads/)?main", self.controls["readiness"])
        self.assertNotIn("(?:refs/heads/)?main", self.controls["audit"])
        self.assertNotIn(
            "https://raw.githubusercontent.com/RobinMJD/IntuneBrew/"
            "main/supported_apps.json",
            self.all_scripts,
        )
        self.assertIn(
            "ConvertTo-CommitManifestUri -Uri ([string]$_) -Commit $catalogCommit",
            self.controls["readiness"],
        )
        self.assertIn(
            '"https://raw.githubusercontent.com/RobinMJD/IntuneBrew/'
            '$Commit/Apps/$([Uri]::EscapeDataString($fileName))"',
            self.controls["readiness"],
        )

    def test_canary_approval_is_bound_and_target_is_revalidated(self):
        self.assertIn("[ValidateSet('Canary', 'Scheduled')]", self.production)
        self.assertIn("$ExecutionMode = 'Canary'", self.production)
        self.assertIn(
            "$ApprovedCatalogCommit -ne $catalogState.CatalogCommit",
            self.production,
        )
        self.assertIn(
            "$ApprovedMarkerCommit -ne $catalogState.MarkerCommit",
            self.production,
        )
        self.assertIn("$_.IntuneAppId, $ApprovedIntuneAppId", self.production)
        self.assertIn("$appsToUpload.Count -ne 1", self.production)
        self.assertLess(
            self.production.index("$appsToUpload.Count -ne 1"),
            self.production.index("if ($updatesAvailable.Count -eq 0)"),
        )
        self.assertIn(
            "$targetBeforePatch = Get-ValidatedIntuneTarget -App $app",
            self.production,
        )
        target_validation = self.production[
            self.production.index("function Get-ValidatedIntuneTarget") :
            self.production.index("function Get-IntuneApps")
        ]
        for field in (
            "primaryBundleId",
            "Test-CompatibleIntuneDisplayName",
            "'@odata.type'",
            "primaryBundleVersion",
            "includedApps",
        ):
            self.assertIn(field, target_validation)
        self.assertIn("-ResponseHeadersVariable responseHeaders", target_validation)
        self.assertNotIn("Microsoft Graph returned no ETag", target_validation)
        self.assertIn("'If-Match' = $targetEtag", self.production)
        self.assertIn(
            "if (-not [string]::IsNullOrWhiteSpace($targetEtag))",
            self.production,
        )
        first_revalidation = self.production.index(
            "Get-ValidatedIntuneTarget -App $app | Out-Null"
        )
        first_mutation = self.production.index(
            "$contentVersion = Invoke-MgGraphRequest -Method POST"
        )
        final_revalidation = self.production.index(
            "$targetBeforePatch = Get-ValidatedIntuneTarget -App $app"
        )
        final_patch = self.production.index("Invoke-MgGraphRequest @patchParameters")
        self.assertLess(first_revalidation, first_mutation)
        self.assertLess(first_mutation, final_revalidation)
        self.assertLess(final_revalidation, final_patch)

    def test_exact_match_requires_name_bundle_and_graph_type(self):
        for name, script in {
            "production": self.production,
            "audit": self.controls["audit"],
        }.items():
            with self.subTest(script=name):
                exact_match = re.search(
                    r"\$matches\s*=\s*@\(\$[a-zA-Z]+Apps\s*\|\s*Where-Object\s*\{"
                    r"(?P<body>.*?)\}\)",
                    script,
                    re.DOTALL,
                )
                self.assertIsNotNone(exact_match)
                body = exact_match.group("body")
                self.assertIn("primaryBundleId", body)
                self.assertIn("Test-CompatibleIntuneDisplayName", body)
                self.assertIn("'@odata.type'", body)
                self.assertGreaterEqual(body.count("-and"), 2)
                self.assertIn("$matches.Count -gt 1", script)
                self.assertIn("$matches.Count -eq 0", script)
                self.assertIn("$partialMatches", script)

        self.assertIn("UNSAFE_MATCH_SKIPPED", self.production)
        self.assertIn("PartialMatchSkipped", self.controls["audit"])
        compact_production = re.sub(r"\s+", "", self.production)
        self.assertIn("$_.IntuneVersion-ne'NotinIntune'", compact_production)

    def test_private_storage_uses_managed_identity_only(self):
        expected_base = (
            "https://intcybintunebrewprd01st.blob.core.windows.net/pkg"
        )
        self.assertIn(expected_base, self.production)
        self.assertIn(expected_base, self.controls["readiness"])
        self.assertIn(expected_base, self.controls["audit"])
        self.assertIn(
            "Get-AzAccessToken -ResourceUrl 'https://storage.azure.com/'",
            self.production,
        )
        self.assertIn(
            "Get-AzAccessToken -ResourceUrl 'https://storage.azure.com/'",
            self.controls["readiness"],
        )
        for forbidden in (
            "DefaultEndpointsProtocol=",
            "AccountKey=",
            "SharedAccessSignature=",
            "BlobEndpoint=",
            "Get-AzStorageAccountKey",
        ):
            self.assertNotIn(forbidden, self.all_scripts)

    def test_package_url_filename_hash_and_length_guards(self):
        manifest_validation = self.production[
            self.production.index("$packageUri = [Uri][string]$response.url") :
            self.production.index("return @{", self.production.index("$packageUri ="))
        ]
        for guard in (
            "$packageUri.Scheme -ne 'https'",
            "-not $packageUri.IsDefaultPort",
            "$packageUri.UserInfo",
            "$packageUri.Query",
            "$packageUri.Fragment",
            "^[0-9a-fA-F]{64}$",
            "Test-SafeLeafFileName",
        ):
            self.assertIn(guard, manifest_validation)
        self.assertIn("IntuneBrew-$([guid]::NewGuid().ToString('N'))", self.production)
        self.assertIn("positive Content-Length", self.production)
        self.assertIn("$actualSize -ne $expectedSize", self.production)

    def test_version_and_filename_behavior(self):
        pwsh = shutil.which("pwsh")
        self.assertIsNotNone(pwsh, "pwsh is required for runbook behavior tests")
        harness = f"""
$ErrorActionPreference = 'Stop'
function Get-RunbookFunction {{
    param([string]$Path, [string]$Name)
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        $Path, [ref]$tokens, [ref]$errors
    )
    if ($errors.Count -gt 0) {{ throw ($errors -join [Environment]::NewLine) }}
    $functionAst = $ast.Find({{
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $Name
    }}, $true)
    if ($null -eq $functionAst) {{ throw "Function not found: $Name" }}
    return $functionAst.Extent.Text
}}
$path = '{PRODUCTION_PATH.as_posix()}'
Invoke-Expression (Get-RunbookFunction $path 'Compare-VersionSegments')
Invoke-Expression (Get-RunbookFunction $path 'Is-NewerVersion')
Invoke-Expression (Get-RunbookFunction $path 'Test-SafeLeafFileName')
Invoke-Expression (Get-RunbookFunction $path 'Test-CompatibleIntuneDisplayName')
Invoke-Expression (Get-RunbookFunction $path 'ConvertTo-CommitManifestUri')
if (Is-NewerVersion '01.002.3' '1.2.3') {{ throw 'Normalized equal versions compared newer' }}
if (-not (Is-NewerVersion '1.0,5000000000' '1.0,4000000000')) {{ throw '64-bit build comparison failed' }}
if (Is-NewerVersion '1.0,4000000000' '1.0,5000000000') {{ throw 'Older 64-bit build compared newer' }}
if (-not (Test-CompatibleIntuneDisplayName 'Firefox' '[CA-SON] Firefox')) {{ throw 'Prefix normalization failed' }}
if (Test-CompatibleIntuneDisplayName 'Firefox' '[CA-SON] Firefox ESR') {{ throw 'Non-exact normalized name matched' }}
if (-not (Test-SafeLeafFileName 'safe-package.pkg')) {{ throw 'Safe PKG rejected' }}
foreach ($unsafe in '../escape.pkg', 'folder/app.dmg', 'CON.pkg', 'trailing.pkg.') {{
    if (Test-SafeLeafFileName $unsafe) {{ throw "Unsafe filename accepted: $unsafe" }}
}}
$commit = '0123456789abcdef0123456789abcdef01234567'
$legacyUri = 'https://raw.githubusercontent.com/ugurkocde/IntuneBrew/main/Apps/1password.json'
$resolvedUri = ConvertTo-CommitManifestUri -Uri $legacyUri -Commit $commit
$expectedUri = "https://raw.githubusercontent.com/RobinMJD/IntuneBrew/$commit/Apps/1password.json"
if ($resolvedUri -ne $expectedUri) {{ throw "Legacy catalog URI was not pinned to the trusted fork: $resolvedUri" }}
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".ps1", encoding="utf-8", delete=False
        ) as script_file:
            script_file.write(harness)
            harness_path = Path(script_file.name)
        try:
            result = subprocess.run(
                [pwsh, "-NoLogo", "-NoProfile", "-File", str(harness_path)],
                check=False,
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                result.returncode,
                0,
                f"PowerShell behavior harness failed:\n{result.stderr}",
            )
        finally:
            harness_path.unlink(missing_ok=True)

    def test_polling_is_bounded_and_failures_surface(self):
        self.assertIn("$maxWaitAttempts = 12", self.production)
        self.assertIn("$maxPollAttempts = 60", self.production)
        self.assertIn("$pollAttempt -lt $maxPollAttempts", self.production)
        self.assertIn("throw \"$processingFailureCount application update(s) failed", self.production)

    def test_logo_assignments_and_included_apps_are_preserved(self):
        self.assertEqual(self.production.count("Add-IntuneAppLogo"), 1)
        self.assertIn("CopyAssignments must remain false", self.production)
        self.assertIn(
            "Existing app logo and assignments were left unchanged",
            self.production,
        )
        self.assertIn("ExistingIncludedApps = $existingIncludedApps", self.production)
        self.assertIn(
            "$existingIncludedApps = @($targetBeforePatch.includedApps)",
            self.production,
        )
        self.assertIn('$updateData["includedApps"]', self.production)
        self.assertIn("$includedBundleVersion", self.production)
        self.assertIn("[string]$_.bundleVersion", self.production)

    def test_control_runbooks_have_expected_read_only_roles(self):
        readiness = self.controls["readiness"]
        audit = self.controls["audit"]
        monitor = self.controls["monitor"]
        self.assertIn("Invoke-MgGraphRequest -Method GET", readiness)
        self.assertIn("$storageHeaders['Range'] = 'bytes=0-0'", readiness)
        self.assertIn("ReadOnly                 = $true", audit)
        for script in (readiness, audit, monitor):
            self.assertNotRegex(
                script,
                re.compile(
                    r"Invoke-MgGraphRequest\s+-Method\s+(POST|PATCH|DELETE)",
                    re.IGNORECASE,
                ),
            )
        self.assertIn(
            "commits?path=IntuneBrew_Runbook.ps1&per_page=1",
            monitor,
        )

    def test_upstream_attribution_and_license_are_retained(self):
        header = "\n".join(self.production.splitlines()[:40])
        self.assertIn("Author:         Ugur Koc", header)
        self.assertIn("License:        MIT", header)
        self.assertIn("github.com/sponsors/ugurkocde", header)


if __name__ == "__main__":
    unittest.main()
