# Azure Automation runbooks

These files version the reviewed Azure Automation control plane around
[`IntuneBrew_Runbook.ps1`](../IntuneBrew_Runbook.ps1). The root script is the
production source monitored by Azure and is intentionally kept at its
upstream-compatible path.

## Architecture and runbook roles

| Runbook | Role | Mutation |
| --- | --- | --- |
| `IntuneBrew_Runbook.ps1` | Validates one immutable catalog snapshot, reads all Intune macOS PKG/DMG apps, and updates one approved canary or at most three scheduled uniquely matched existing apps. | Content/version only |
| `IntuneBrew-Readiness.ps1` | Checks Graph read access, catalog marker/run trust, every catalog URL, and a managed-identity authenticated one-byte package read. | None |
| `IntuneBrew-UpdateAudit.ps1` | Produces update candidates with the same exact name, bundle ID, Graph type, manifest, and version rules as production. | None |
| `IntuneBrew-UpstreamMonitor.ps1` | Compares the approved source commit with the latest commit that changed the root runbook and checks catalog freshness. | None |

The production runbook trusts only `RobinMJD/IntuneBrew`. It resolves
`.github/catalog-state.json` from that path's commit history, requires the
marker parent to equal `catalogCommit`, and verifies the referenced Actions run
is completed, successful, on `main`, from the expected workflow, and from an
eligible event. It then uses that commit for the complete catalog and all
manifests. Mutable `main` content is never used during an update.

## Runtime and modules

- Azure Automation PowerShell 7.x runtime.
- `Microsoft.Graph.Authentication` 2.38.1 or newer.
- `Az.Accounts` for managed-identity Azure Storage token acquisition.
- A system-assigned managed identity is the reviewed production
  authentication mode.

Import compatible module versions into the Automation account before running
readiness. Keep the control runbooks and root runbook on the same reviewed
commit.

## Automation variables

| Variable | Required value or purpose |
| --- | --- |
| `AuthenticationMethod` | `SystemManagedIdentity` for the reviewed deployment |
| `UseExistingIntuneApp` | Boolean `true`; any other value blocks production |
| `CopyAssignments` | Boolean `false`; assignments remain attached in place |
| `MaxAppsPerRun` | Integer from `1` through `3` |
| `IntuneBrewCatalogMaxAgeHours` | Positive maximum accepted marker age |
| `IntuneBrewPackageStorageBaseUrl` | Exactly `https://intcybintunebrewprd01st.blob.core.windows.net/pkg` |
| `IntuneBrewSourceCommit` | Lowercase 40-character reviewed root-runbook commit |

Production accepts `ExecutionMode` (`Canary` by default or `Scheduled`) plus
`ApprovedCatalogCommit`, `ApprovedMarkerCommit`, and `ApprovedIntuneAppId`
parameters. Canary mode requires all three approvals to match the current
trusted catalog state and exactly one current update candidate. The reviewed
deployment rejects every authentication mode except `SystemManagedIdentity`;
do not configure the retained upstream legacy authentication branches.

## Permissions and private storage

Grant the Automation account managed identity the Microsoft Graph application
role `DeviceManagementApps.ReadWrite.All`. The readiness and audit runbooks use
the same identity for Graph reads; the monitor requires no Graph role.

Grant the managed identity `Storage Blob Data Reader` on the private `pkg`
container (or the narrowest equivalent scope). Package reads request a bearer
token for `https://storage.azure.com/`; do not use connection strings, account
keys, SAS URLs, or anonymous container access. Package URLs must use HTTPS on
the default port with no userinfo, query, or fragment.

## Update-only safety

Production has no app-creation path. Before any write it:

1. Loads and validates the entire commit-addressed catalog once.
2. Reads a complete paginated Graph snapshot of macOS DMG/PKG apps.
3. Requires one exact match by prefix-normalized display name, bundle ID, and
   expected Graph type.
4. Validates the package filename, SHA256, URL, positive `Content-Length`, and
   downloaded length inside an isolated temporary directory.
5. Re-reads the target, requires an ETag, creates and commits only a new content
   version, then patches version fields with `If-Match`.

Zero, multiple, partial, duplicate, or incomplete mappings are skipped or fail
closed before mutation. Existing assignments and logo are not changed.
Existing `includedApps` entries are retained, with only the matching primary
bundle version updated. Upload, commit, and patch failures fail the Automation
job; polling is bounded.

## Scheduling and canary promotion

Keep production schedules disabled until readiness is `READY`, the audit is
`TrustedForApproval`, the catalog marker is fresh, and the source monitor is
healthy. Schedule controls first, then production with no overlap with catalog
publication. A stale marker, source drift, ambiguous match, invalid manifest,
or failed catalog run is a scheduling gate.

For a canary, run readiness, review the audit candidate, and start production
manually with `ExecutionMode=Canary` and the audit's exact catalog commit,
marker commit, and Intune app ID. Confirm the existing Intune app ID,
assignments, logo, included-app detection, committed content, and reported
version before enabling any schedule. Scheduled mode may use
`MaxAppsPerRun` only up to `3`.

To promote a reviewed root-runbook commit:

1. Merge a reviewed PR that changes `IntuneBrew_Runbook.ps1`.
2. Record the resulting `main` commit SHA.
3. Import that exact root file revision into the Azure runbook without edits.
4. Set `IntuneBrewSourceCommit` to the same SHA.
5. Run readiness, audit, and the source monitor, then perform the one-app canary.

Never promote while the catalog publication workflow is still pushing its
final catalog-state marker.
