#Requires -Modules @{ ModuleName = 'Microsoft.Graph.Authentication'; ModuleVersion = '2.38.1' }

param(
    [ValidateRange(1, 100)]
    [int]$CandidateLimit = 25
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Invoke-GitHubApi {
    param([Parameter(Mandatory)][string]$Uri)

    $headers = @{
        Accept                 = 'application/vnd.github+json'
        'X-GitHub-Api-Version' = '2022-11-28'
        'User-Agent'           = 'Sonepar-IntuneAutomation'
    }

    for ($attempt = 1; $attempt -le 4; $attempt++) {
        try {
            return Invoke-RestMethod -Uri $Uri -Method Get -Headers $headers
        }
        catch {
            $statusCode = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
            $transient = $statusCode -in 408, 429, 500, 502, 503, 504
            if (-not $transient -or $attempt -eq 4) {
                throw
            }

            $delaySeconds = [Math]::Min(30, 5 * [Math]::Pow(2, $attempt - 1))
            Write-Warning "GitHub API returned HTTP $statusCode; retrying in $delaySeconds seconds (attempt $attempt of 4)."
            Start-Sleep -Seconds $delaySeconds
        }
    }
}

function Save-GitHubArchive {
    param(
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][string]$Path
    )

    for ($attempt = 1; $attempt -le 4; $attempt++) {
        try {
            Invoke-WebRequest -Uri $Uri -OutFile $Path -Headers @{
                'User-Agent' = 'Sonepar-IntuneAutomation'
            }
            return
        }
        catch {
            $statusCode = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
            $transient = $statusCode -in 408, 429, 500, 502, 503, 504
            if (-not $transient -or $attempt -eq 4) {
                throw
            }

            if (Test-Path -LiteralPath $Path) {
                Remove-Item -LiteralPath $Path -Force
            }
            $delaySeconds = [Math]::Min(30, 5 * [Math]::Pow(2, $attempt - 1))
            Write-Warning "GitHub archive download returned HTTP $statusCode; retrying in $delaySeconds seconds (attempt $attempt of 4)."
            Start-Sleep -Seconds $delaySeconds
        }
    }
}

function Get-PublishedCatalogState {
    param([Parameter(Mandatory)][double]$MaximumAgeHours)

    $stateCommits = @((Invoke-GitHubApi -Uri 'https://api.github.com/repos/RobinMJD/IntuneBrew/commits?path=.github/catalog-state.json&per_page=1'))
    if ($stateCommits.Count -ne 1 -or [string]$stateCommits[0].sha -notmatch '^[0-9a-f]{40}$') {
        throw 'GitHub returned no valid catalog-state marker commit.'
    }
    $markerCommit = [string]$stateCommits[0].sha
    $markerCommitDetails = Invoke-GitHubApi -Uri "https://api.github.com/repos/RobinMJD/IntuneBrew/commits/$markerCommit"
    if (@($markerCommitDetails.parents).Count -ne 1 -or
        [string]$markerCommitDetails.parents[0].sha -notmatch '^[0-9a-f]{40}$') {
        throw 'The catalog-state marker commit does not have one valid parent.'
    }
    $state = Invoke-RestMethod -Uri "https://raw.githubusercontent.com/RobinMJD/IntuneBrew/$markerCommit/.github/catalog-state.json" -Method Get
    if ([int]$state.schemaVersion -ne 1 -or
        [string]$state.repository -ne 'RobinMJD/IntuneBrew' -or
        [string]$state.workflowName -ne 'Build App Packages and Collect App Information' -or
        [string]$state.workflowPath -ne '.github/workflows/build-app-packages.yml' -or
        [string]$state.packageStorageBaseUrl -ne 'https://intcybintunebrewprd01st.blob.core.windows.net/pkg' -or
        [string]$state.catalogCommit -notmatch '^[0-9a-f]{40}$' -or
        [long]$state.runId -le 0 -or
        [string]$state.publishedAt -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$') {
        throw 'The catalog-state marker schema or values are invalid.'
    }
    if ([string]$markerCommitDetails.parents[0].sha -ne [string]$state.catalogCommit) {
        throw 'The catalog-state marker does not identify its exact parent commit.'
    }

    $run = Invoke-GitHubApi -Uri "https://api.github.com/repos/RobinMJD/IntuneBrew/actions/runs/$([long]$state.runId)"
    if ([long]$run.id -ne [long]$state.runId -or
        [string]$run.status -ne 'completed' -or
        [string]$run.conclusion -ne 'success' -or
        [string]$run.name -ne [string]$state.workflowName -or
        [string]$run.path -ne [string]$state.workflowPath -or
        [string]$run.head_branch -ne 'main' -or
        [string]$run.event -notin 'schedule', 'workflow_dispatch', 'push' -or
        [string]$run.repository.full_name -ne [string]$state.repository) {
        throw 'The catalog-state marker does not reference a successful eligible workflow run.'
    }
    $publishedAt = [DateTimeOffset]::ParseExact(
        [string]$state.publishedAt,
        'yyyy-MM-ddTHH:mm:ssZ',
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::AssumeUniversal
    )
    $totalAgeHours = ([DateTimeOffset]::UtcNow - $publishedAt).TotalHours
    if ($totalAgeHours -lt 0) {
        throw 'The catalog-state publication timestamp is in the future.'
    }
    $ageHours = [Math]::Round($totalAgeHours, 1)
    [pscustomobject]@{
        Fresh               = $totalAgeHours -le $MaximumAgeHours
        LastSuccessAgeHours = $ageHours
        LatestConclusion    = 'success'
        CatalogCommit       = [string]$state.catalogCommit
        MarkerCommit        = $markerCommit
        RunId               = [long]$state.runId
        RunUrl              = [string]$run.html_url
    }
}

function Compare-VersionSegments {
    param(
        [Parameter(Mandatory)][string]$VersionA,
        [Parameter(Mandatory)][string]$VersionB
    )

    $partsA = $VersionA -split '\.'
    $partsB = $VersionB -split '\.'
    $maximumLength = [Math]::Max($partsA.Length, $partsB.Length)

    for ($index = 0; $index -lt $maximumLength; $index++) {
        $segmentA = if ($index -lt $partsA.Length) { $partsA[$index].Trim() } else { '0' }
        $segmentB = if ($index -lt $partsB.Length) { $partsB[$index].Trim() } else { '0' }
        $numberA = [int64]0
        $numberB = [int64]0
        $numericA = [int64]::TryParse($segmentA, [ref]$numberA)
        $numericB = [int64]::TryParse($segmentB, [ref]$numberB)

        if ($numericA -and $numericB) {
            if ($numberA -gt $numberB) { return 1 }
            if ($numberA -lt $numberB) { return -1 }
        }
        else {
            if (-not [string]::Equals($segmentA, $segmentB, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Version comparison is indeterminate for nonnumeric segments '$segmentA' and '$segmentB'."
            }
        }
    }

    return 0
}

function Test-NewerVersion {
    param(
        [Parameter(Mandatory)][string]$AvailableVersion,
        [Parameter(Mandatory)][string]$CurrentVersion
    )

    try {
        $available = $AvailableVersion -replace '-.*$'
        $current = $CurrentVersion -replace '-.*$'
        $availableParts = $available -split ','
        $currentParts = $current -split ','
        $mainComparison = Compare-VersionSegments `
            -VersionA ($availableParts[0] -replace '\s*\(.*\)$', '') `
            -VersionB ($currentParts[0] -replace '\s*\(.*\)$', '')

        if ($mainComparison -ne 0) {
            return $mainComparison -gt 0
        }

        if ($availableParts.Length -gt 1 -and $currentParts.Length -gt 1) {
            $availableBuild = [int64]0
            $currentBuild = [int64]0
            if ([int64]::TryParse($availableParts[1].Trim(), [ref]$availableBuild) -and
                [int64]::TryParse($currentParts[1].Trim(), [ref]$currentBuild)) {
                return $availableBuild -gt $currentBuild
            }
            return $false
        }

        return $false
    }
    catch {
        Write-Warning "Could not compare available version '$AvailableVersion' with current version '$CurrentVersion'."
        return $false
    }
}

function Convert-VersionToSortable {
    param([AllowEmptyString()][string]$Version)

    $segments = ($Version -replace '-.*$' -replace '\s*\(.*\)$', '') -split '[.,]'
    $padded = foreach ($segment in $segments) {
        $number = [int64]0
        if ([int64]::TryParse($segment.Trim(), [ref]$number)) {
            $number.ToString('D12')
        }
        else {
            $segment
        }
    }

    $padded -join '.'
}

function Test-CompatibleIntuneDisplayName {
    param(
        [AllowEmptyString()][string]$CatalogName,
        [AllowEmptyString()][string]$IntuneDisplayName
    )

    if ([string]::IsNullOrWhiteSpace($CatalogName) -or [string]::IsNullOrWhiteSpace($IntuneDisplayName)) {
        return $false
    }
    $normalizedDisplayName = $IntuneDisplayName.Trim() -replace '^(?:\[[^\]]+\]\s*)+', ''
    [string]::Equals($normalizedDisplayName, $CatalogName.Trim(), [StringComparison]::OrdinalIgnoreCase)
}

function Test-SafeLeafFileName {
    param([AllowEmptyString()][string]$Name)

    if ([string]::IsNullOrWhiteSpace($Name) -or
        [IO.Path]::IsPathRooted($Name) -or
        $Name -ne [IO.Path]::GetFileName($Name) -or
        $Name -match '[<>:"/\\|?*\x00-\x1F]' -or
        $Name.TrimEnd(' ', '.') -ne $Name) {
        return $false
    }

    $stem = [IO.Path]::GetFileNameWithoutExtension($Name)
    $reservedNames = @('CON', 'PRN', 'AUX', 'NUL') +
        @(1..9 | ForEach-Object { "COM$_" }) +
        @(1..9 | ForEach-Object { "LPT$_" })
    return $stem.ToUpperInvariant() -notin $reservedNames
}

$maximumAgeHours = [double](Get-AutomationVariable -Name 'IntuneBrewCatalogMaxAgeHours')
$packageStorageBaseUrl = [string](Get-AutomationVariable -Name 'IntuneBrewPackageStorageBaseUrl')
if ($packageStorageBaseUrl.TrimEnd('/') -ne 'https://intcybintunebrewprd01st.blob.core.windows.net/pkg') {
    throw 'IntuneBrewPackageStorageBaseUrl does not identify the approved private package container.'
}
$packageStorageBaseUri = [Uri]$packageStorageBaseUrl
$catalogHealth = Get-PublishedCatalogState -MaximumAgeHours $maximumAgeHours
$catalogCommit = $catalogHealth.CatalogCommit
$archiveUri = "https://codeload.github.com/RobinMJD/IntuneBrew/zip/$catalogCommit"
$temporaryName = "IntuneBrewAudit-$([guid]::NewGuid().ToString('N'))"
$archivePath = Join-Path $env:TEMP "$temporaryName.zip"
$extractPath = Join-Path $env:TEMP $temporaryName
$connected = $false

try {
    Save-GitHubArchive -Uri $archiveUri -Path $archivePath
    Expand-Archive -LiteralPath $archivePath -DestinationPath $extractPath

    $repositoryRoot = Get-ChildItem -LiteralPath $extractPath -Directory | Select-Object -First 1
    if ($null -eq $repositoryRoot) {
        throw 'The downloaded catalog archive did not contain a repository folder.'
    }

    $catalogPath = Join-Path $repositoryRoot.FullName 'supported_apps.json'
    $appsPath = Join-Path $repositoryRoot.FullName 'Apps'
    $catalog = Get-Content -LiteralPath $catalogPath -Raw | ConvertFrom-Json
    $manifestUris = @($catalog.PSObject.Properties.Value)
    if ($manifestUris.Count -eq 0) {
        throw 'The supported-app catalog is empty.'
    }
    $manifests = [System.Collections.Generic.List[object]]::new()
    $manifestFailures = [System.Collections.Generic.List[string]]::new()
    $catalogIdentityKeys = @{}

    foreach ($manifestUriValue in $manifestUris) {
        $manifestUri = [string]$manifestUriValue
        if ($manifestUri -notmatch '^https://raw\.githubusercontent\.com/RobinMJD/IntuneBrew/main/Apps/.+\.json$') {
            $manifestFailures.Add("$manifestUri (unexpected URL)")
            continue
        }

        try {
            $manifestUriObject = [Uri]$manifestUri
            $decodedManifestPath = [Uri]::UnescapeDataString($manifestUriObject.AbsolutePath)
            $manifestFileName = [IO.Path]::GetFileName($decodedManifestPath)
            if (-not (Test-SafeLeafFileName -Name $manifestFileName) -or
                $manifestFileName -notmatch '\.json$' -or
                $decodedManifestPath -notmatch "^/RobinMJD/IntuneBrew/main/Apps/$([regex]::Escape($manifestFileName))$") {
                throw 'The manifest URI does not resolve to one safe Apps filename.'
            }
            $appsRoot = [IO.Path]::GetFullPath($appsPath).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
            $manifestPath = [IO.Path]::GetFullPath((Join-Path $appsPath $manifestFileName))
            if (-not $manifestPath.StartsWith($appsRoot, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'The resolved manifest path escapes the Apps directory.'
            }
            $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
            if ([string]::IsNullOrWhiteSpace([string]$manifest.name) -or
                [string]::IsNullOrWhiteSpace([string]$manifest.version) -or
                [string]::IsNullOrWhiteSpace([string]$manifest.bundleId) -or
                [string]::IsNullOrWhiteSpace([string]$manifest.url) -or
                [string]::IsNullOrWhiteSpace([string]$manifest.fileName) -or
                [string]::IsNullOrWhiteSpace([string]$manifest.sha)) {
                throw 'A required name, version, bundleId, url, fileName, or sha value is missing.'
            }

            $packageUri = [Uri][string]$manifest.url
            if ($packageUri.Scheme -ne 'https' -or
                -not $packageUri.IsDefaultPort -or
                -not [string]::IsNullOrEmpty($packageUri.UserInfo) -or
                -not [string]::IsNullOrEmpty($packageUri.Query) -or
                -not [string]::IsNullOrEmpty($packageUri.Fragment)) {
                throw "The package URL is not an approved HTTPS URL: $($manifest.url)"
            }
            if ($packageUri.Host -eq 'intunebrew.blob.core.windows.net') {
                throw 'The manifest still references the upstream IntuneBrew package cache.'
            }
            if ($packageUri.Host -eq $packageStorageBaseUri.Host) {
                $approvedPathPrefix = $packageStorageBaseUri.AbsolutePath.TrimEnd('/') + '/'
                if (-not $packageUri.AbsolutePath.StartsWith($approvedPathPrefix, [StringComparison]::Ordinal)) {
                    throw 'The manifest references an unexpected path in the private package storage account.'
                }
            }
            if ([string]$manifest.sha -notmatch '^[0-9a-fA-F]{64}$') {
                throw 'The manifest SHA256 value is invalid.'
            }
            if (-not (Test-SafeLeafFileName -Name ([string]$manifest.fileName)) -or
                [string]$manifest.fileName -notmatch '\.(?:dmg|pkg)$') {
                throw 'The manifest package filename is invalid.'
            }

            $identityKey = "$(([string]$manifest.name).Trim().ToLowerInvariant())`0$(([string]$manifest.bundleId).Trim().ToLowerInvariant())"
            if ($catalogIdentityKeys.ContainsKey($identityKey)) {
                throw "Duplicate catalog identity also used by $($catalogIdentityKeys[$identityKey])."
            }
            $catalogIdentityKeys[$identityKey] = [string]$manifest.name

            $manifests.Add([pscustomobject]@{
                Name        = [string]$manifest.name
                Version     = [string]$manifest.version
                BundleId    = [string]$manifest.bundleId
                PackageUrl  = [string]$manifest.url
                FileName    = [string]$manifest.fileName
                IsPrivatePackage = $packageUri.Host -eq $packageStorageBaseUri.Host
                ManifestUri = $manifestUri
            })
        }
        catch {
            $manifestFailures.Add("$manifestUri ($($_.Exception.Message))")
        }
    }

    if ($manifests.Count -ne $manifestUris.Count -or $manifestFailures.Count -gt 0) {
        throw "Catalog validation failed: $($manifests.Count) of $($manifestUris.Count) manifests loaded."
    }

    Import-Module Microsoft.Graph.Authentication -MinimumVersion 2.38.1 -ErrorAction Stop
    Connect-MgGraph -Identity -NoWelcome -ErrorAction Stop
    $connected = $true

    $intuneApps = [System.Collections.Generic.List[object]]::new()
    $graphUri = "https://graph.microsoft.com/beta/deviceAppManagement/mobileApps?`$filter=(isof('microsoft.graph.macOSDmgApp') or isof('microsoft.graph.macOSPkgApp'))"

    while ($graphUri) {
        $page = Invoke-MgGraphRequest -Method GET -Uri $graphUri -ErrorAction Stop
        if ($null -eq $page -or $page.PSObject.Properties.Name -notcontains 'value') {
            throw "Microsoft Graph returned an invalid mobile-app page for $graphUri"
        }
        foreach ($app in @($page.value)) {
            if ([string]::IsNullOrWhiteSpace([string]$app.id) -or
                [string]::IsNullOrWhiteSpace([string]$app.displayName) -or
                [string]::IsNullOrWhiteSpace([string]$app.primaryBundleId) -or
                [string]::IsNullOrWhiteSpace([string]$app.'@odata.type')) {
                throw 'Microsoft Graph returned an incomplete macOS app record.'
            }
            $intuneApps.Add($app)
        }
        $graphUri = [string]$page.'@odata.nextLink'
    }

    $candidates = [System.Collections.Generic.List[object]]::new()
    $unsafeMatches = [System.Collections.Generic.List[object]]::new()
    $matchedIntuneAppIds = @{}
    $blockingMatchFailures = 0
    foreach ($manifest in $manifests) {
        $expectedODataType = if ($manifest.FileName -match '\.dmg$') {
            '#microsoft.graph.macOSDmgApp'
        }
        else {
            '#microsoft.graph.macOSPkgApp'
        }
        $matches = @($intuneApps | Where-Object {
            [string]::Equals(([string]$_.primaryBundleId).Trim(), $manifest.BundleId.Trim(), [StringComparison]::OrdinalIgnoreCase) -and
            (Test-CompatibleIntuneDisplayName -CatalogName $manifest.Name -IntuneDisplayName ([string]$_.displayName)) -and
            [string]::Equals([string]$_.'@odata.type', $expectedODataType, [StringComparison]::OrdinalIgnoreCase)
        })

        if ($matches.Count -gt 1) {
            $blockingMatchFailures++
            $unsafeMatches.Add([pscustomobject]@{
                AppName            = $manifest.Name
                BundleId           = $manifest.BundleId
                IntuneDisplayNames = (@($matches.displayName) -join ' | ')
                Reason             = 'MultipleExactMatches'
            })
            continue
        }

        if ($matches.Count -eq 0) {
            $partialMatches = @($intuneApps | Where-Object {
                [string]::Equals(([string]$_.primaryBundleId).Trim(), $manifest.BundleId.Trim(), [StringComparison]::OrdinalIgnoreCase) -or
                (Test-CompatibleIntuneDisplayName -CatalogName $manifest.Name -IntuneDisplayName ([string]$_.displayName))
            })
            if ($partialMatches.Count -gt 0) {
                $unsafeMatches.Add([pscustomobject]@{
                    AppName            = $manifest.Name
                    BundleId           = $manifest.BundleId
                    IntuneDisplayNames = (@($partialMatches.displayName) -join ' | ')
                    Reason             = 'PartialMatchSkipped'
                })
            }
            continue
        }

        $installedApp = $matches[0]
        $intuneAppId = [string]$installedApp.id
        if ([string]::IsNullOrWhiteSpace($intuneAppId)) {
            $blockingMatchFailures++
            Write-Warning "Blocking '$($manifest.Name)' because its matched Intune app ID is empty."
            continue
        }
        if ($matchedIntuneAppIds.ContainsKey($intuneAppId)) {
            $blockingMatchFailures++
            $unsafeMatches.Add([pscustomobject]@{
                AppName            = $manifest.Name
                BundleId           = $manifest.BundleId
                IntuneDisplayNames = [string]$installedApp.displayName
                Reason             = "IntuneAppAlreadyMappedTo:$($matchedIntuneAppIds[$intuneAppId])"
            })
            continue
        }
        $matchedIntuneAppIds[$intuneAppId] = $manifest.Name

        $currentVersion = [string]$installedApp.primaryBundleVersion
        if ([string]::IsNullOrWhiteSpace($currentVersion)) {
            $blockingMatchFailures++
            Write-Warning "Blocking '$($manifest.Name)' because its Intune primaryBundleVersion is empty."
            continue
        }
        $invalidIncludedApps = @(@($installedApp.includedApps) | Where-Object {
            [string]::IsNullOrWhiteSpace([string]$_.bundleId) -or
            [string]::IsNullOrWhiteSpace([string]$_.bundleVersion)
        })
        if ($invalidIncludedApps.Count -gt 0) {
            $blockingMatchFailures++
            Write-Warning "Blocking '$($manifest.Name)' because it has incomplete included-app detection data."
            continue
        }

        if (Test-NewerVersion -AvailableVersion $manifest.Version -CurrentVersion $currentVersion) {
            $candidates.Add([pscustomobject]@{
                AppName           = $manifest.Name
                IntuneDisplayName = [string]$installedApp.displayName
                CurrentVersion    = $currentVersion
                AvailableVersion  = $manifest.Version
                MatchType         = 'NormalizedNameAndBundleId'
                IntuneAppId       = $intuneAppId
                ManifestUri       = $manifest.ManifestUri
            })
        }
    }

    $sortedCandidates = @($candidates | Sort-Object AppName)
    foreach ($candidate in @($sortedCandidates | Select-Object -First $CandidateLimit)) {
        Write-Output ('CANDIDATE ' + ($candidate | ConvertTo-Json -Compress))
    }
    foreach ($unsafeMatch in @($unsafeMatches | Select-Object -First 10)) {
        Write-Warning ('UNSAFE_MATCH_SKIPPED ' + ($unsafeMatch | ConvertTo-Json -Compress))
    }
    foreach ($manifestFailure in @($manifestFailures | Select-Object -First 10)) {
        Write-Warning "MANIFEST_FAILURE $manifestFailure"
    }

    $trustedForApproval = $catalogHealth.Fresh -and
        $manifestFailures.Count -eq 0 -and
        $manifests.Count -eq $manifestUris.Count -and
        $blockingMatchFailures -eq 0
    [pscustomobject]@{
        Check                    = 'IntuneBrewUpdateAudit'
        ReadOnly                 = $true
        CatalogSourceCommit      = $catalogCommit
        CatalogMarkerCommit      = $catalogHealth.MarkerCommit
        CatalogRunId             = $catalogHealth.RunId
        CatalogRunUrl            = $catalogHealth.RunUrl
        CatalogFresh             = $catalogHealth.Fresh
        CatalogAgeHours          = $catalogHealth.LastSuccessAgeHours
        CatalogMaxAgeHours       = $maximumAgeHours
        LatestCatalogConclusion  = $catalogHealth.LatestConclusion
        CatalogManifestCount     = $manifestUris.Count
        CatalogManifestsRead     = $manifests.Count
        CatalogManifestFailures  = $manifestFailures.Count
        PrivatePackageManifests  = @($manifests | Where-Object IsPrivatePackage).Count
        IntuneMacAppsRead        = $intuneApps.Count
        UpdateCandidateCount     = $sortedCandidates.Count
        CandidatesReported       = [Math]::Min($sortedCandidates.Count, $CandidateLimit)
        UnsafeMatchesSkipped     = $unsafeMatches.Count
        BlockingMatchFailures    = $blockingMatchFailures
        TrustedForApproval       = $trustedForApproval
        CheckedAtUtc             = [DateTimeOffset]::UtcNow
    } | ConvertTo-Json -Compress

    if (-not $trustedForApproval) {
        Write-Warning 'AUDIT_NOT_APPROVABLE: The candidate list is informational only because the catalog is stale, a manifest is invalid, or a match is ambiguous.'
    }
}
finally {
    if ($connected) {
        Disconnect-MgGraph -ErrorAction SilentlyContinue | Out-Null
    }
    if (Test-Path -LiteralPath $archivePath) {
        Remove-Item -LiteralPath $archivePath -Force
    }
    if (Test-Path -LiteralPath $extractPath) {
        Remove-Item -LiteralPath $extractPath -Recurse -Force
    }
}
