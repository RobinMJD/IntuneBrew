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
        Fresh         = $totalAgeHours -le $MaximumAgeHours
        AgeHours      = $ageHours
        CatalogCommit = [string]$state.catalogCommit
        MarkerCommit  = $markerCommit
        RunId         = [long]$state.runId
        RunEvent      = [string]$run.event
        RunUrl        = [string]$run.html_url
    }
}

$approvedCommit = [string](Get-AutomationVariable -Name 'IntuneBrewSourceCommit')
$maximumAgeHours = [double](Get-AutomationVariable -Name 'IntuneBrewCatalogMaxAgeHours')
if ($approvedCommit -notmatch '^[0-9a-f]{40}$') {
    throw 'IntuneBrewSourceCommit is missing or is not a valid lowercase 40-character commit SHA.'
}
$sourceUri = 'https://api.github.com/repos/RobinMJD/IntuneBrew/commits?path=IntuneBrew_Runbook.ps1&per_page=1'

$latestSourceCommit = @((Invoke-GitHubApi -Uri $sourceUri))[0]
if ($null -eq $latestSourceCommit) {
    throw 'GitHub returned no commit for IntuneBrew_Runbook.ps1.'
}
if ([string]$latestSourceCommit.sha -notmatch '^[0-9a-f]{40}$') {
    throw 'GitHub returned an invalid latest IntuneBrew_Runbook.ps1 commit SHA.'
}

$catalogState = Get-PublishedCatalogState -MaximumAgeHours $maximumAgeHours

$sourceUpdateAvailable = [string]$latestSourceCommit.sha -ne $approvedCommit
$catalogFresh = $catalogState.Fresh
$blockedReasons = [System.Collections.Generic.List[string]]::new()

if ($sourceUpdateAvailable) {
    $blockedReasons.Add("SOURCE_UPDATE_AVAILABLE: approved=$approvedCommit latest=$($latestSourceCommit.sha) url=$($latestSourceCommit.html_url)")
}

if (-not $catalogFresh) {
    $blockedReasons.Add("CATALOG_STALE: published catalog state is $($catalogState.AgeHours) hours old; maximum is $maximumAgeHours hours. Run: $($catalogState.RunUrl)")
}

[pscustomobject]@{
    Check                    = 'IntuneBrewUpstreamMonitor'
    ApprovedSourceCommit     = $approvedCommit
    LatestSourceCommit       = [string]$latestSourceCommit.sha
    SourceUpdateAvailable    = $sourceUpdateAvailable
    LatestSourceCommitUrl    = [string]$latestSourceCommit.html_url
    LatestSourceCommitDate   = [DateTimeOffset]$latestSourceCommit.commit.committer.date
    CatalogFresh             = $catalogFresh
    CatalogAgeHours          = $catalogState.AgeHours
    CatalogMaxAgeHours       = $maximumAgeHours
    CatalogCommit            = $catalogState.CatalogCommit
    CatalogMarkerCommit      = $catalogState.MarkerCommit
    LatestCatalogConclusion  = 'success'
    LatestCatalogEvent       = $catalogState.RunEvent
    LatestCatalogRunId       = $catalogState.RunId
    LatestCatalogRunUrl      = $catalogState.RunUrl
    CheckedAtUtc             = [DateTimeOffset]::UtcNow
} | ConvertTo-Json -Compress

if ($blockedReasons.Count -gt 0) {
    foreach ($reason in $blockedReasons) {
        Write-Error -Message $reason -ErrorAction Continue
    }
    throw "UPSTREAM_MONITOR_BLOCKED: $($blockedReasons.Count) condition(s) require review."
}

Write-Output 'UPSTREAM_MONITOR_HEALTHY'
