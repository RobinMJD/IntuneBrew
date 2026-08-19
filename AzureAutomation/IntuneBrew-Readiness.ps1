#Requires -Modules @{ ModuleName = 'Microsoft.Graph.Authentication'; ModuleVersion = '2.38.1' }

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

function Get-StorageAuthorizationHeaders {
    Import-Module Az.Accounts -ErrorAction Stop
    Disable-AzContextAutosave -Scope Process | Out-Null
    Connect-AzAccount -Identity -ErrorAction Stop | Out-Null
    $tokenResponse = Get-AzAccessToken -ResourceUrl 'https://storage.azure.com/' -ErrorAction Stop
    $token = $tokenResponse.Token
    if ($token -is [securestring]) {
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($token)
        try {
            $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        }
        finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
    }

    if ([string]::IsNullOrWhiteSpace([string]$token)) {
        throw 'Managed identity returned an empty Azure Storage access token.'
    }

    @{
        Authorization  = "Bearer $token"
        'x-ms-version' = '2023-11-03'
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

    $stateUri = "https://raw.githubusercontent.com/RobinMJD/IntuneBrew/$markerCommit/.github/catalog-state.json"
    $state = Invoke-RestMethod -Uri $stateUri -Method Get
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
        Fresh           = $totalAgeHours -le $MaximumAgeHours
        MaximumAgeHours = $MaximumAgeHours
        AgeHours        = $ageHours
        PublishedAtUtc  = $publishedAt
        CatalogCommit   = [string]$state.catalogCommit
        MarkerCommit    = $markerCommit
        RunId           = [long]$state.runId
        RunEvent        = [string]$run.event
        RunUrl          = [string]$run.html_url
    }
}

$maximumAgeHours = [double](Get-AutomationVariable -Name 'IntuneBrewCatalogMaxAgeHours')
$packageStorageBaseUrl = [string](Get-AutomationVariable -Name 'IntuneBrewPackageStorageBaseUrl')
$packageStorageBaseUri = [Uri]$packageStorageBaseUrl
if ($packageStorageBaseUrl.TrimEnd('/') -ne 'https://intcybintunebrewprd01st.blob.core.windows.net/pkg' -or
    $packageStorageBaseUri.Scheme -ne 'https' -or
    -not $packageStorageBaseUri.IsDefaultPort -or
    -not [string]::IsNullOrEmpty($packageStorageBaseUri.UserInfo) -or
    -not [string]::IsNullOrEmpty($packageStorageBaseUri.Query) -or
    -not [string]::IsNullOrEmpty($packageStorageBaseUri.Fragment)) {
    throw 'IntuneBrewPackageStorageBaseUrl does not identify the approved private package container.'
}
$catalogState = Get-PublishedCatalogState -MaximumAgeHours $maximumAgeHours
$catalogCommit = $catalogState.CatalogCommit
$catalogUri = "https://raw.githubusercontent.com/RobinMJD/IntuneBrew/$catalogCommit/supported_apps.json"
$connected = $false

try {
    Import-Module Microsoft.Graph.Authentication -MinimumVersion 2.38.1 -ErrorAction Stop
    Connect-MgGraph -Identity -NoWelcome -ErrorAction Stop
    $connected = $true

    $context = Get-MgContext
    if ($null -eq $context) {
        throw 'Managed identity authentication returned no Microsoft Graph context.'
    }

    $graphUri = 'https://graph.microsoft.com/beta/deviceAppManagement/mobileApps?$top=1&$select=id,displayName'
    $graphResponse = Invoke-MgGraphRequest -Method GET -Uri $graphUri -ErrorAction Stop
    $sampleApps = @($graphResponse.value)

    $catalog = Invoke-RestMethod -Uri $catalogUri -Method Get
    $manifestUris = @($catalog.PSObject.Properties.Value)
    if ($manifestUris.Count -eq 0) {
        throw 'The IntuneBrew supported-app catalog is empty.'
    }
    $invalidManifestUris = @($manifestUris | Where-Object {
        [string]$_ -notmatch '^https://raw\.githubusercontent\.com/RobinMJD/IntuneBrew/main/Apps/.+\.json$'
    })
    if ($invalidManifestUris.Count -gt 0) {
        throw "The catalog contains $($invalidManifestUris.Count) unexpected manifest URL(s)."
    }
    $duplicateManifestUris = @($manifestUris | Group-Object | Where-Object Count -gt 1)
    if ($duplicateManifestUris.Count -gt 0) {
        throw "The catalog contains $($duplicateManifestUris.Count) duplicate manifest URL(s)."
    }

    $sampleManifestUri = [string]$manifestUris[0]
    $commitAddressedManifestUri = $sampleManifestUri.Replace('/main/', "/$catalogCommit/")
    $sampleManifest = Invoke-RestMethod -Uri $commitAddressedManifestUri -Method Get
    foreach ($requiredProperty in 'name', 'version', 'url', 'fileName', 'sha') {
        if ($sampleManifest.PSObject.Properties.Name -notcontains $requiredProperty) {
            throw "The sample catalog manifest is missing '$requiredProperty'."
        }
    }

    $packageProbeCatalogUri = [string]($manifestUris | Where-Object {
        [string]$_ -match '/Apps/1password\.json$'
    } | Select-Object -First 1)
    if ([string]::IsNullOrWhiteSpace($packageProbeCatalogUri)) {
        throw 'The catalog does not contain the required private-package probe manifest.'
    }
    $packageProbeManifestUri = $packageProbeCatalogUri.Replace('/main/', "/$catalogCommit/")
    $packageProbeManifest = Invoke-RestMethod -Uri $packageProbeManifestUri -Method Get
    $packageProbeUrl = [string]$packageProbeManifest.url
    $packageProbeUri = [Uri]$packageProbeUrl
    if ($packageProbeUri.Scheme -ne 'https' -or
        -not $packageProbeUri.IsDefaultPort -or
        -not [string]::IsNullOrEmpty($packageProbeUri.UserInfo) -or
        $packageProbeUri.Host -ne $packageStorageBaseUri.Host -or
        -not $packageProbeUri.AbsolutePath.StartsWith($packageStorageBaseUri.AbsolutePath.TrimEnd('/') + '/', [StringComparison]::Ordinal) -or
        -not [string]::IsNullOrEmpty($packageProbeUri.Query) -or
        -not [string]::IsNullOrEmpty($packageProbeUri.Fragment)) {
        throw "The catalog package probe does not use the approved private package container: $packageProbeUrl"
    }

    $storageHeaders = Get-StorageAuthorizationHeaders
    $storageHeaders['Range'] = 'bytes=0-0'
    $packageProbe = Invoke-WebRequest -Uri $packageProbeUrl -Method Get -Headers $storageHeaders
    if ($packageProbe.StatusCode -ne 206 -or $packageProbe.RawContentLength -ne 1) {
        throw "Managed identity ranged package read returned HTTP $($packageProbe.StatusCode) and $($packageProbe.RawContentLength) byte(s)."
    }

    $readinessStatus = if ($catalogState.Fresh) { 'READY' } else { 'BLOCKED' }

    [pscustomobject]@{
        Check                  = 'IntuneBrewReadiness'
        Status                 = $readinessStatus
        GraphAuthentication    = 'SystemManagedIdentity'
        GraphReadSucceeded     = $true
        GraphSampleRecordCount = $sampleApps.Count
        CatalogSourceCommit    = $catalogCommit
        CatalogManifestCount   = $manifestUris.Count
        CatalogSampleName      = [string]$sampleManifest.name
        PrivatePackageRead     = $true
        PrivatePackageProbe    = [string]$packageProbeManifest.name
        PrivatePackageBytes    = [long]$packageProbe.RawContentLength
        CatalogMarkerCommit    = $catalogState.MarkerCommit
        CatalogRunId           = $catalogState.RunId
        CatalogFresh           = $catalogState.Fresh
        CatalogAgeHours        = $catalogState.AgeHours
        CatalogMaxAgeHours     = $catalogState.MaximumAgeHours
        LatestCatalogRun       = 'success'
        LatestCatalogRunUrl    = $catalogState.RunUrl
        CheckedAtUtc           = [DateTimeOffset]::UtcNow
    } | ConvertTo-Json -Compress

    if (-not $catalogState.Fresh) {
        Write-Warning ("CATALOG_STALE: Published catalog state is {0} hours old; maximum allowed age is {1} hours." -f $catalogState.AgeHours, $catalogState.MaximumAgeHours)
    }
}
finally {
    if ($connected) {
        Disconnect-MgGraph -ErrorAction SilentlyContinue | Out-Null
    }
    Disconnect-AzAccount -Scope Process -ErrorAction SilentlyContinue | Out-Null
}
