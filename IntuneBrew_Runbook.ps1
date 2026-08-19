<#
.SYNOPSIS
    This is for you to schedule and run IntuneBrew via a Azure Runbook. 
    
    This will upload all available updates for all macOS applications in Intune.  
.DESCRIPTION
    IntuneBrew is an automation script that streamlines the process of uploading and updating macOS applications
    in Microsoft Intune. It leverages a curated repository of popular applications to ensure your organization's
    apps are always up to date.

    Key Features:
    - Automated version checking against current Intune deployments
    - Secure file downloads with SHA256 verification
    - Automatic app encryption for Intune compatibility
    - Built-in error handling and retry mechanisms
    - Detailed logging for troubleshooting
    - Support for both .pkg and .dmg file formats
    - Automatic logo management for deployed applications

.NOTES
    Version:        0.2
    Author:         Ugur Koc
    Creation Date:  2025-02-24
    Updated:        2026-01-14
    Repository:     https://github.com/RobinMJD/IntuneBrew
    License:        MIT

.LINK
    Project Homepage: https://github.com/RobinMJD/IntuneBrew
    Issue Tracker:    https://github.com/RobinMJD/IntuneBrew/issues
    Sponsor:          https://github.com/sponsors/ugurkocde

.REQUIREMENTS
    - Azure Automation Account with Managed Identity
    - Required Graph API Permissions:
        * DeviceManagementApps.ReadWrite.All
    - PowerShell 7.0 or later
    - Microsoft.Graph.Authentication module
#>
param(
    [ValidateSet('Canary', 'Scheduled')]
    [string]$ExecutionMode = 'Canary',
    [string]$ApprovedCatalogCommit,
    [string]$ApprovedMarkerCommit,
    [string]$ApprovedIntuneAppId
)

# Disable verbose output to avoid cluttering the Azure Automation Runbook logs
$VerbosePreference = "SilentlyContinue"

# Function to write logs that will be visible in Azure Automation
function Write-Log {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message,
        [Parameter(Mandatory = $false)]
        [string]$Type = "Info"  # Info, Warning, Error, Verbose
    )
    
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $logMessage = "[$timestamp] [$Type] $Message"
    if ($Type -eq "Verbose") {
        # Enable verbose output only when we really need it
        $VerbosePreference = "Continue"
        Write-Verbose $logMessage
        $VerbosePreference = "SilentlyContinue"
    }
    else {
        Write-Output $logMessage
    }
}

function Invoke-GitHubRequestWithRetry {
    param([Parameter(Mandatory = $true)][string]$Uri)

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
            Write-Log "GitHub returned HTTP $statusCode; retrying in $delaySeconds seconds (attempt $attempt of 4)." -Type "Warning"
            Start-Sleep -Seconds $delaySeconds
        }
    }
}

function Get-PublishedCatalogState {
    param([Parameter(Mandatory = $true)][double]$MaximumAgeHours)

    $stateCommits = @((Invoke-GitHubRequestWithRetry -Uri 'https://api.github.com/repos/RobinMJD/IntuneBrew/commits?path=.github/catalog-state.json&per_page=1'))
    if ($stateCommits.Count -ne 1 -or [string]$stateCommits[0].sha -notmatch '^[0-9a-f]{40}$') {
        throw 'GitHub returned no valid catalog-state marker commit.'
    }
    $markerCommit = [string]$stateCommits[0].sha
    $markerCommitDetails = Invoke-GitHubRequestWithRetry -Uri "https://api.github.com/repos/RobinMJD/IntuneBrew/commits/$markerCommit"
    if (@($markerCommitDetails.parents).Count -ne 1 -or
        [string]$markerCommitDetails.parents[0].sha -notmatch '^[0-9a-f]{40}$') {
        throw 'The catalog-state marker commit does not have one valid parent.'
    }
    $state = Invoke-GitHubRequestWithRetry -Uri "https://raw.githubusercontent.com/RobinMJD/IntuneBrew/$markerCommit/.github/catalog-state.json"
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
    $run = Invoke-GitHubRequestWithRetry -Uri "https://api.github.com/repos/RobinMJD/IntuneBrew/actions/runs/$([long]$state.runId)"
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
        RunUrl        = [string]$run.html_url
    }
}

function Test-PrivatePackageUrl {
    param([Parameter(Mandatory = $true)][string]$Uri)

    try {
        $targetUri = [Uri]$Uri
        $baseUri = $script:PackageStorageBaseUri
        $basePath = $baseUri.AbsolutePath.TrimEnd('/') + '/'
        return $targetUri.Scheme -eq 'https' -and
            $targetUri.IsDefaultPort -and
            [string]::IsNullOrEmpty($targetUri.UserInfo) -and
            $targetUri.Host -eq $baseUri.Host -and
            $targetUri.AbsolutePath.StartsWith($basePath, [StringComparison]::Ordinal) -and
            [string]::IsNullOrEmpty($targetUri.Query) -and
            [string]::IsNullOrEmpty($targetUri.Fragment)
    }
    catch {
        return $false
    }
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

function ConvertTo-CommitManifestUri {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$Commit
    )

    $sourceUri = [Uri]$Uri
    $decodedPath = [Uri]::UnescapeDataString($sourceUri.AbsolutePath)
    $fileName = [IO.Path]::GetFileName($decodedPath)
    if ($sourceUri.Scheme -ne 'https' -or
        -not $sourceUri.IsDefaultPort -or
        -not [string]::IsNullOrEmpty($sourceUri.UserInfo) -or
        $sourceUri.Host -ne 'raw.githubusercontent.com' -or
        -not [string]::IsNullOrEmpty($sourceUri.Query) -or
        -not [string]::IsNullOrEmpty($sourceUri.Fragment) -or
        -not (Test-SafeLeafFileName -Name $fileName) -or
        $fileName -notmatch '\.json$' -or
        $decodedPath -notmatch "^/RobinMJD/IntuneBrew/main/Apps/$([regex]::Escape($fileName))$" -or
        $Commit -notmatch '^[0-9a-f]{40}$') {
        throw "Unexpected manifest URL in supported_apps.json: $Uri"
    }

    "https://raw.githubusercontent.com/RobinMJD/IntuneBrew/$Commit/Apps/$([Uri]::EscapeDataString($fileName))"
}

function Get-StorageAuthorizationHeaders {
    if (-not $script:AzIdentityConnected) {
        Import-Module Az.Accounts -ErrorAction Stop
        Disable-AzContextAutosave -Scope Process | Out-Null
        Connect-AzAccount -Identity -ErrorAction Stop | Out-Null
        $script:AzIdentityConnected = $true
    }

    if ($null -eq $script:StorageAccessToken -or
        [DateTimeOffset]::UtcNow.AddMinutes(5) -ge $script:StorageAccessTokenExpiresOn) {
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

        $script:StorageAccessToken = [string]$token
        $script:StorageAccessTokenExpiresOn = [DateTimeOffset]$tokenResponse.ExpiresOn
    }

    @{
        Authorization  = "Bearer $($script:StorageAccessToken)"
        'x-ms-version' = '2023-11-03'
    }
}

function Invoke-PackageWebRequest {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][ValidateSet('Head', 'Get')][string]$Method,
        [string]$OutFile
    )

    $parameters = @{
        Uri         = $Uri
        Method      = $Method
        ErrorAction = 'Stop'
    }
    if (-not [string]::IsNullOrWhiteSpace($OutFile)) {
        $parameters.OutFile = $OutFile
    }
    if (Test-PrivatePackageUrl -Uri $Uri) {
        $parameters.Headers = Get-StorageAuthorizationHeaders
    }

    Invoke-WebRequest @parameters
}

Write-Log "Starting IntuneBrew Automation Runbook - Version 0.2"

# Authentication START

# Required Graph API permissions for app functionality
$requiredPermissions = @(
    "DeviceManagementApps.ReadWrite.All"
)

# Get the authentication method from Automation Account variable
$AuthenticationMethod = Get-AutomationVariable -Name 'AuthenticationMethod'
$CopyAssignments = Get-AutomationVariable -Name 'CopyAssignments' -ErrorAction SilentlyContinue
if ($AuthenticationMethod -ne 'SystemManagedIdentity') {
    throw "AuthenticationMethod must be 'SystemManagedIdentity' for this deployment."
}
Import-Module Microsoft.Graph.Authentication -MinimumVersion 2.38.1 -ErrorAction Stop
Disconnect-MgGraph -ErrorAction SilentlyContinue | Out-Null

# Get UseExistingIntuneApp setting - when true, updates existing apps instead of creating new ones (Issue #141)
$UseExistingIntuneApp = Get-AutomationVariable -Name 'UseExistingIntuneApp' -ErrorAction SilentlyContinue
if ($null -eq $UseExistingIntuneApp) { $UseExistingIntuneApp = $false }

# Get MaxAppsPerRun setting - limits apps processed per run to prevent memory exhaustion (Issue #45)
$MaxAppsPerRun = Get-AutomationVariable -Name 'MaxAppsPerRun' -ErrorAction SilentlyContinue
if ($null -eq $MaxAppsPerRun -or $MaxAppsPerRun -lt 1 -or $MaxAppsPerRun -gt 3) {
    throw 'MaxAppsPerRun must be explicitly configured between 1 and 3.'
}
$CatalogMaxAgeHours = Get-AutomationVariable -Name 'IntuneBrewCatalogMaxAgeHours' -ErrorAction SilentlyContinue
if ($null -eq $CatalogMaxAgeHours -or $CatalogMaxAgeHours -le 0) {
    throw "IntuneBrewCatalogMaxAgeHours must be configured with a positive value."
}
$ApprovedSourceCommit = Get-AutomationVariable -Name 'IntuneBrewSourceCommit' -ErrorAction SilentlyContinue
$PackageStorageBaseUrl = [string](Get-AutomationVariable -Name 'IntuneBrewPackageStorageBaseUrl' -ErrorAction SilentlyContinue)
try {
    $script:PackageStorageBaseUri = [Uri]$PackageStorageBaseUrl
}
catch {
    throw 'IntuneBrewPackageStorageBaseUrl must be a valid absolute URI.'
}
if ($script:PackageStorageBaseUri.Scheme -ne 'https' -or
    -not $script:PackageStorageBaseUri.IsDefaultPort -or
    -not [string]::IsNullOrEmpty($script:PackageStorageBaseUri.UserInfo) -or
    $script:PackageStorageBaseUri.Host -ne 'intcybintunebrewprd01st.blob.core.windows.net' -or
    $script:PackageStorageBaseUri.AbsolutePath.TrimEnd('/') -ne '/pkg' -or
    -not [string]::IsNullOrEmpty($script:PackageStorageBaseUri.Query) -or
    -not [string]::IsNullOrEmpty($script:PackageStorageBaseUri.Fragment)) {
    throw 'IntuneBrewPackageStorageBaseUrl must identify the approved private pkg container over HTTPS.'
}
$script:AzIdentityConnected = $false
$script:StorageAccessToken = $null
$script:StorageAccessTokenExpiresOn = [DateTimeOffset]::MinValue
$script:WorkingDirectory = $null

if ($UseExistingIntuneApp -ne $true) {
    throw 'UseExistingIntuneApp must be explicitly set to true. This deployment never creates new Intune apps.'
}
if ($CopyAssignments -eq $true) {
    throw 'CopyAssignments must remain false when updating existing Intune apps in place.'
}

if ($CopyAssignments -eq $true) {
    Write-Log "Copy Assignments is set to true"
    $requiredPermissions += "Group.Read.All"
}

# Don't copy assignments if updating existing app (assignments are preserved on existing app)
if ($UseExistingIntuneApp -eq $true) {
    Write-Log "UseExistingIntuneApp is set to true - will update existing apps instead of creating new ones"
    if ($CopyAssignments -eq $true) {
        Write-Log "Note: CopyAssignments is ignored when UseExistingIntuneApp is enabled (assignments are preserved)" -Type "Warning"
        $CopyAssignments = $false
    }
}

# Log configuration summary
Write-Log "Configuration Summary:"
Write-Log "  - Authentication Method: $AuthenticationMethod"
Write-Log "  - Execution Mode: $ExecutionMode"
Write-Log "  - Copy Assignments: $CopyAssignments"
Write-Log "  - Use Existing Intune App: $UseExistingIntuneApp"
Write-Log "  - Max Apps Per Run: $MaxAppsPerRun"
Write-Log "  - Catalog Maximum Age: $CatalogMaxAgeHours hours"
Write-Log "  - Approved Upstream Source: $ApprovedSourceCommit"
Write-Log "  - Private Package Storage: $($script:PackageStorageBaseUri.AbsoluteUri.TrimEnd('/'))"

# Check if the AuthenticationMethod variable is empty
if ([string]::IsNullOrWhiteSpace($AuthenticationMethod)) {
    Write-Log "Authentication method is not specified. Please set the 'AuthenticationMethod' Automation Account variable." -Type "Error"
    throw "Authentication method is required but not provided."
}

# Use Client Secret for authentication (Issues #108, #103)
if ($AuthenticationMethod -eq "ClientSecret") {
    Write-Log "Using Client Secret for authentication"

    # Get authentication details from Automation Account variables
    try {
        $tenantId = Get-AutomationVariable -Name 'TenantId'
        $appId = Get-AutomationVariable -Name 'AppId'
        $clientSecret = Get-AutomationVariable -Name 'ClientSecret'

        Write-Log "Successfully retrieved authentication variables from Automation Account"
    }
    catch {
        Write-Log "Failed to retrieve authentication variables: $_" -Type "Error"
        throw
    }

    # Clear any existing connections to prevent token cache issues
    try {
        Disconnect-MgGraph -ErrorAction SilentlyContinue
    }
    catch {
        # Ignore disconnect errors - there may not be an existing connection
    }

    # Authenticate using client secret from Automation Account
    $authSuccess = $false

    # Primary method: PSCredential approach
    try {
        Write-Log "Attempting primary authentication method (PSCredential)..."
        $SecureClientSecret = ConvertTo-SecureString -String $clientSecret -AsPlainText -Force
        $ClientSecretCredential = New-Object -TypeName System.Management.Automation.PSCredential -ArgumentList $appId, $SecureClientSecret
        Connect-MgGraph -TenantId $tenantId -ClientSecretCredential $ClientSecretCredential -NoWelcome -ErrorAction Stop
        Write-Log "Successfully connected to Microsoft Graph using client secret authentication"
        $authSuccess = $true
    }
    catch {
        Write-Log "Primary authentication method failed: $_" -Type "Warning"
        Write-Log "Attempting fallback authentication method (direct token acquisition)..." -Type "Warning"

        # Fallback method: Direct OAuth2 token acquisition via REST API
        try {
            $tokenBody = @{
                Grant_Type    = "client_credentials"
                Scope         = "https://graph.microsoft.com/.default"
                Client_Id     = $appId
                Client_Secret = $clientSecret
            }
            $tokenResponse = Invoke-RestMethod -Uri "https://login.microsoftonline.com/$tenantId/oauth2/v2.0/token" -Method Post -Body $tokenBody -ContentType "application/x-www-form-urlencoded"
            $accessToken = $tokenResponse.access_token

            # Connect with acquired token
            $secureToken = ConvertTo-SecureString -String $accessToken -AsPlainText -Force
            Connect-MgGraph -AccessToken $secureToken -NoWelcome -ErrorAction Stop
            Write-Log "Successfully connected using fallback token acquisition method"
            $authSuccess = $true
        }
        catch {
            Write-Log "Fallback authentication also failed: $_" -Type "Error"
        }
    }

    if (-not $authSuccess) {
        Write-Log "Failed to authenticate with Microsoft Graph using client secret" -Type "Error"
        Write-Log "Troubleshooting steps:" -Type "Error"
        Write-Log "  1. Verify TenantId, AppId, and ClientSecret are correct" -Type "Error"
        Write-Log "  2. Ensure ClientSecret has not expired" -Type "Error"
        Write-Log "  3. Verify the app registration has required Graph API permissions" -Type "Error"
        Write-Log "  4. Check if the ClientSecret value (not ID) is stored in automation variable" -Type "Error"
        throw "Failed to authenticate with Microsoft Graph using client secret"
    }

    # Log module version for troubleshooting
    $graphModule = Get-Module Microsoft.Graph.Authentication -ErrorAction SilentlyContinue
    if ($graphModule) {
        Write-Log "Microsoft.Graph.Authentication version: $($graphModule.Version)"
    }
}
# Use System Managed Identity for authentication (Issue #43 - improved error handling)
elseif ($AuthenticationMethod -eq "SystemManagedIdentity") {
    Write-Log "Using System Managed Identity for authentication"

    # Authenticate using System Managed Identity from Automation Account
    try {
        Connect-MgGraph -Identity -NoWelcome -ErrorAction Stop
        Write-Log "Successfully connected to Microsoft Graph using System Managed Identity"

        # Log module version for troubleshooting
        $graphModule = Get-Module Microsoft.Graph.Authentication -ErrorAction SilentlyContinue
        if ($graphModule) {
            Write-Log "Microsoft.Graph.Authentication version: $($graphModule.Version)"
        }
    }
    catch {
        Write-Log "Failed to connect to Microsoft Graph using System Managed Identity. Error: $_" -Type "Error"
        Write-Log " " -Type "Error"
        Write-Log "Troubleshooting steps for System Managed Identity:" -Type "Error"
        Write-Log "  1. Go to Azure Portal > Automation Account > Identity" -Type "Error"
        Write-Log "  2. Ensure 'System assigned' tab shows Status: On" -Type "Error"
        Write-Log "  3. Click 'Azure role assignments' and verify Graph API permissions:" -Type "Error"
        Write-Log "     - DeviceManagementApps.ReadWrite.All" -Type "Error"
        Write-Log "     - Group.Read.All (if using CopyAssignments)" -Type "Error"
        Write-Log "  4. If permissions were recently added, wait 5-10 minutes for propagation" -Type "Error"
        Write-Log " " -Type "Error"
        Write-Log "To assign permissions via PowerShell:" -Type "Error"
        Write-Log "  Connect-AzAccount" -Type "Error"
        Write-Log "  \$MI = Get-AzADServicePrincipal -DisplayName '<AutomationAccountName>'" -Type "Error"
        Write-Log "  \$GraphApp = Get-AzADServicePrincipal -Filter \"appId eq '00000003-0000-0000-c000-000000000000'\"" -Type "Error"
        Write-Log "  \$Permission = \$GraphApp.AppRole | Where-Object {{\$_.Value -eq 'DeviceManagementApps.ReadWrite.All'}}" -Type "Error"
        Write-Log "  New-AzADServicePrincipalAppRoleAssignment -ServicePrincipalId \$MI.Id -ResourceId \$GraphApp.Id -AppRoleId \$Permission.Id" -Type "Error"
        throw
    }
}
# Use User Assigned Managed Identity for authentication (Issue #43 - improved error handling)
elseif ($AuthenticationMethod -eq "UserAssignedManagedIdentity") {
    Write-Log "Using User Assigned Managed Identity for authentication"

    # Authenticate using User Assigned Managed Identity from Automation Account
    try {
        $appId = Get-AutomationVariable -Name 'AppId'
        if ([string]::IsNullOrWhiteSpace($appId)) {
            throw "AppId automation variable is not set. Required for User Assigned Managed Identity."
        }
        Write-Log "Using Client ID: $appId"

        Connect-MgGraph -Identity -ClientId $appId -NoWelcome -ErrorAction Stop
        Write-Log "Successfully connected to Microsoft Graph using User Assigned Managed Identity"

        # Log module version for troubleshooting
        $graphModule = Get-Module Microsoft.Graph.Authentication -ErrorAction SilentlyContinue
        if ($graphModule) {
            Write-Log "Microsoft.Graph.Authentication version: $($graphModule.Version)"
        }
    }
    catch {
        Write-Log "Failed to connect to Microsoft Graph using User Assigned Managed Identity. Error: $_" -Type "Error"
        Write-Log " " -Type "Error"
        Write-Log "Troubleshooting steps for User Assigned Managed Identity:" -Type "Error"
        Write-Log "  1. Go to Azure Portal > Automation Account > Identity" -Type "Error"
        Write-Log "  2. Switch to 'User assigned' tab" -Type "Error"
        Write-Log "  3. Click 'Add' and select your User Assigned Managed Identity" -Type "Error"
        Write-Log "  4. Ensure the 'AppId' automation variable contains the Client ID of your User Assigned Identity" -Type "Error"
        Write-Log "  5. Verify the User Assigned Identity has Graph API permissions:" -Type "Error"
        Write-Log "     - DeviceManagementApps.ReadWrite.All" -Type "Error"
        Write-Log "     - Group.Read.All (if using CopyAssignments)" -Type "Error"
        Write-Log " " -Type "Error"
        Write-Log "To find the Client ID of your User Assigned Identity:" -Type "Error"
        Write-Log "  Go to Azure Portal > Managed Identities > Select your identity > Overview > Client ID" -Type "Error"
        throw
    }
}

# Check and display the current permissions
$context = Get-MgContext
if ($null -eq $context) {
    Write-Log "Failed to get Graph context - authentication may have failed silently" -Type "Error"
    throw "No active Graph connection. Please verify authentication succeeded."
}
$currentPermissions = $context.Scopes

# Validate required permissions
$missingPermissions = $requiredPermissions | Where-Object { $_ -notin $currentPermissions }
if ($missingPermissions.Count -gt 0) {
    Write-Log "WARNING: Missing required permissions:" -Type "Warning"
    foreach ($permission in $missingPermissions) {
        Write-Log "  - $permission" -Type "Warning"
    }
    Write-Log "Please ensure these permissions are granted to the app registration" -Type "Warning"
    throw "Missing required permissions"
}

Write-Log "All required permissions are present"

# Authentication END

# Import required modules
Import-Module Microsoft.Graph.Authentication

# Encrypts app file using AES encryption for Intune upload
# Fixed with proper resource disposal to prevent memory leaks (Issue #45)
function EncryptFile($sourceFile) {
    function GenerateKey() {
        $aesSp = [System.Security.Cryptography.AesCryptoServiceProvider]::new()
        try {
            $aesSp.GenerateKey()
            return $aesSp.Key
        }
        finally {
            $aesSp.Dispose()
        }
    }

    $targetFile = "$sourceFile.bin"

    # Initialize all disposable objects to $null for proper cleanup in finally block
    $sha256 = $null
    $aes = $null
    $hmac = $null
    $sourceStream = $null
    $targetStream = $null
    $cryptoStream = $null
    $transform = $null

    # Store values needed for return object before cleanup
    $encryptionKey = $null
    $fileDigest = $null
    $initializationVector = $null
    $macValue = $null
    $macKey = $null

    try {
        $sha256 = [System.Security.Cryptography.SHA256]::Create()
        $aes = [System.Security.Cryptography.Aes]::Create()
        $aes.Key = GenerateKey
        $hmac = [System.Security.Cryptography.HMACSHA256]::new()
        $hmac.Key = GenerateKey
        $hashLength = $hmac.HashSize / 8

        $sourceStream = [System.IO.File]::OpenRead($sourceFile)
        $sourceSha256 = $sha256.ComputeHash($sourceStream)
        $sourceStream.Seek(0, "Begin") | Out-Null
        $targetStream = [System.IO.File]::Open($targetFile, "Create")

        $targetStream.Write((New-Object byte[] $hashLength), 0, $hashLength)
        $targetStream.Write($aes.IV, 0, $aes.IV.Length)
        $transform = $aes.CreateEncryptor()
        $cryptoStream = [System.Security.Cryptography.CryptoStream]::new($targetStream, $transform, "Write")
        $sourceStream.CopyTo($cryptoStream)
        $cryptoStream.FlushFinalBlock()

        $targetStream.Seek($hashLength, "Begin") | Out-Null
        $mac = $hmac.ComputeHash($targetStream)
        $targetStream.Seek(0, "Begin") | Out-Null
        $targetStream.Write($mac, 0, $mac.Length)

        # Store values before cleanup
        $encryptionKey = [System.Convert]::ToBase64String($aes.Key)
        $fileDigest = [System.Convert]::ToBase64String($sourceSha256)
        $initializationVector = [System.Convert]::ToBase64String($aes.IV)
        $macValue = [System.Convert]::ToBase64String($mac)
        $macKey = [System.Convert]::ToBase64String($hmac.Key)
    }
    finally {
        # Dispose all resources in reverse order of creation
        if ($cryptoStream) { $cryptoStream.Dispose() }
        if ($targetStream) { $targetStream.Dispose() }
        if ($sourceStream) { $sourceStream.Dispose() }
        if ($transform) { $transform.Dispose() }
        if ($hmac) { $hmac.Dispose() }
        if ($aes) { $aes.Dispose() }
        if ($sha256) { $sha256.Dispose() }
    }

    return [PSCustomObject][ordered]@{
        encryptionKey        = $encryptionKey
        fileDigest           = $fileDigest
        fileDigestAlgorithm  = "SHA256"
        initializationVector = $initializationVector
        mac                  = $macValue
        macKey               = $macKey
        profileIdentifier    = "ProfileVersion1"
    }
}

# Aggressively clears memory to prevent Azure Automation sandbox suspension (Issue #45)
function Clear-MemoryAggressively {
    # Force garbage collection multiple times for thorough cleanup
    for ($i = 0; $i -lt 3; $i++) {
        [System.GC]::Collect()
        [System.GC]::WaitForPendingFinalizers()
    }

    # Force full blocking collection
    [System.GC]::Collect([System.GC]::MaxGeneration, [System.GCCollectionMode]::Forced, $true, $true)

    # Log memory usage for monitoring
    try {
        $process = Get-Process -Id $PID -ErrorAction SilentlyContinue
        if ($process) {
            $memoryMB = [Math]::Round($process.WorkingSet64 / 1MB, 2)
            Write-Log "Current memory usage: $memoryMB MB"
        }
    }
    catch {
        # Ignore errors when getting process info in Azure Automation
    }
}

# Handles chunked upload of large files to Azure Storage
# Renews the Azure Storage SAS URI for an in-progress upload via the Graph renewUpload
# action. Large uploads outlast the SAS token validity window, which failed blocks with
# a signed expiry error and forced full restarts (Issue #154, Issue #87).
function Request-RenewedSasUri {
    param([string]$fileStatusUri)

    if ([string]::IsNullOrEmpty($fileStatusUri)) {
        Write-Log "No content file status URI available to renew the upload URL." -Type "Warning"
        return $null
    }

    try {
        Invoke-MgGraphRequest -Method POST -Uri "$fileStatusUri/renewUpload" -Body "{}" | Out-Null
    }
    catch {
        Write-Log "Upload URL renewal request failed: $($_.Exception.Message)" -Type "Warning"
    }

    for ($renewAttempt = 0; $renewAttempt -lt 6; $renewAttempt++) {
        Start-Sleep -Seconds 5
        $renewedStatus = Invoke-MgGraphRequest -Method GET -Uri $fileStatusUri
        if ($renewedStatus.azureStorageUri) {
            return $renewedStatus.azureStorageUri
        }
    }
    return $null
}

function UploadFileToAzureStorage($sasUri, $filepath) {
    try {
        Write-Log "Starting file upload to Azure Storage"
        $fileSize = [Math]::Round((Get-Item $filepath).Length / 1MB, 2)
        Write-Log "File size: $fileSize MB"

        $blockSize = 8 * 1024 * 1024  # 8 MB block size
        $fileSize = (Get-Item $filepath).Length
        $totalBlocks = [Math]::Ceiling($fileSize / $blockSize)

        $maxRetries = 3
        $retryCount = 0
        $uploadSuccess = $false
        $lastProgressReport = 0

        # Renew the SAS URI proactively during long uploads so blocks never hit the
        # token expiry window (Issue #154)
        $sasRenewalStopwatch = [System.Diagnostics.Stopwatch]::StartNew()
        $sasRenewalIntervalMinutes = 7

        while (-not $uploadSuccess -and $retryCount -lt $maxRetries) {
            try {
                if ($retryCount -gt 0) {
                    Write-Log "Retry attempt $($retryCount + 1) of $maxRetries" -Type "Warning"
                }
                
                $fileStream = [System.IO.File]::OpenRead($filepath)
                $blockId = 0
                $blockList = [System.Xml.Linq.XDocument]::Parse(@"
<?xml version="1.0" encoding="utf-8"?>
<BlockList></BlockList>
"@)
                
                $blockList.Declaration.Encoding = "utf-8"
                $blockBuffer = [byte[]]::new($blockSize)
                
                while ($bytesRead = $fileStream.Read($blockBuffer, 0, $blockSize)) {
                    # Refresh the SAS URI before it expires so multi-GB uploads survive (Issue #154)
                    if ($sasRenewalStopwatch.Elapsed.TotalMinutes -ge $sasRenewalIntervalMinutes) {
                        $renewedUri = Request-RenewedSasUri $fileStatusUri
                        if ($renewedUri) {
                            $sasUri = $renewedUri
                        }
                        $sasRenewalStopwatch.Restart()
                    }

                    $blockIdBytes = [System.Text.Encoding]::UTF8.GetBytes($blockId.ToString("D6"))
                    $id = [System.Convert]::ToBase64String($blockIdBytes)
                    $blockList.Root.Add([System.Xml.Linq.XElement]::new("Latest", $id))

                    $uploadBlockSuccess = $false
                    $blockRetries = 3
                    while (-not $uploadBlockSuccess -and $blockRetries -gt 0) {
                        try {
                            $blockUri = "$sasUri&comp=block&blockid=$id"
                            Invoke-WebRequest -Method Put $blockUri `
                                -Headers @{"x-ms-blob-type" = "BlockBlob" } `
                                -Body ([byte[]]($blockBuffer[0..$($bytesRead - 1)])) `
                                -ErrorAction Stop | Out-Null

                            $uploadBlockSuccess = $true
                        }
                        catch {
                            $blockRetries--
                            if ($blockRetries -gt 0) {
                                # An expired SAS token is the most common block failure on long
                                # uploads - renew it and retry the same block (Issue #154)
                                if ($_.Exception.Message -match "AuthenticationFailed|Signed expiry time|403") {
                                    Write-Log "Upload token expired. Requesting a new upload URL..." -Type "Warning"
                                    $renewedUri = Request-RenewedSasUri $fileStatusUri
                                    if ($renewedUri) {
                                        $sasUri = $renewedUri
                                        $sasRenewalStopwatch.Restart()
                                    }
                                }
                                Start-Sleep -Seconds 2
                            }
                            else {
                                Write-Log "Failed to upload block. Error: $_" -Type "Error"
                            }
                        }
                    }

                    if (-not $uploadBlockSuccess) {
                        throw "Failed to upload block after multiple retries"
                    }

                    $percentComplete = [Math]::Round(($blockId + 1) / $totalBlocks * 100, 1)
                    # Only log progress at 10% intervals
                    if ($percentComplete - $lastProgressReport -ge 10) {
                        Write-Log "Upload progress: $percentComplete%"
                        $lastProgressReport = [Math]::Floor($percentComplete / 10) * 10
                    }
                    
                    $blockId++
                }
                
                $fileStream.Close()

                Write-Log "Finalizing upload..."
                Invoke-RestMethod -Method Put "$sasUri&comp=blocklist" -Body $blockList | Out-Null
                Write-Log "Upload completed successfully"
                
                $uploadSuccess = $true
            }
            catch {
                $retryCount++
                Write-Log "Upload attempt failed: $_" -Type "Error"
                if ($retryCount -lt $maxRetries) {
                    Write-Log "Retrying upload..." -Type "Warning"
                    $renewedUri = Request-RenewedSasUri $fileStatusUri
                    if ($renewedUri) {
                        $sasUri = $renewedUri
                        $sasRenewalStopwatch.Restart()
                        Write-Log "Received new upload URL"
                    }
                    Start-Sleep -Seconds 5
                }
                else {
                    Write-Log "Failed all upload attempts" -Type "Error"
                    throw
                }
            }
            finally {
                if ($fileStream) {
                    $fileStream.Close()
                }
            }
        }
    }
    catch {
        Write-Log "Critical error during upload: $_" -Type "Error"
        throw
    }
}

# Function to get assignments for a specific Intune app
function Get-IntuneAppAssignments {
    param (
        [string]$AppId
    )

    if ([string]::IsNullOrEmpty($AppId)) {
        Write-Log "Error: App ID is required to fetch assignments." -Type "Verbose"
        return $null
    }

    Write-Log "`n🔍 Fetching assignments for existing app (ID: $AppId)..." -Type "Verbose"
    $assignmentsUri = "https://graph.microsoft.com/beta/deviceAppManagement/mobileApps/$AppId/assignments"
    
    try {
        # Use Invoke-MgGraphRequest for consistency and authentication handling
        $response = Invoke-MgGraphRequest -Method GET -Uri $assignmentsUri
        
        # The response directly contains the assignments array in the 'value' property
        if ($response.value -ne $null -and $response.value.Count -gt 0) {
            Write-Log "✅ Found $($response.value.Count) assignment(s)." -Type "Verbose"
            return $response.value
        }
        else {
            Write-Log "ℹ️ No assignments found for the existing app." -Type "Verbose"
            return @() # Return an empty array if no assignments
        }
    }
    catch {
        Write-Log "❌ Error fetching assignments for App ID ${AppId}: $($_.Exception.Message)" -Type "Verbose"
        # Consider returning specific error info or re-throwing if needed
        return $null # Indicate error
    }
}

# Function to apply assignments to a specific Intune app
function Set-IntuneAppAssignments {
    param (
        [string]$NewAppId,
        [array]$Assignments
    )

    if ([string]::IsNullOrEmpty($NewAppId)) {
        Write-Log "Error: New App ID is required to set assignments." -Type "Error"
        return
    }

    # Check if $Assignments is null or empty before proceeding
    if ($Assignments -eq $null -or $Assignments.Count -eq 0) {
        Write-Log "ℹ️ No assignments to apply." -Type "Info"
        return
    }

    Write-Log "Applying assignments to new app (ID: $NewAppId)..." -Type "Info"
    $assignmentsUri = "https://graph.microsoft.com/beta/deviceAppManagement/mobileApps/$NewAppId/assignments"
    $appliedCount = 0
    $failedCount = 0

    foreach ($assignment in $Assignments) {
        # Construct the body for the new assignment
        $targetObject = $null
        $originalTargetType = $assignment.target.'@odata.type'

        # Determine the target type and construct the target object accordingly
        if ($assignment.target.groupId) {
            $targetObject = @{
                "@odata.type" = "#microsoft.graph.groupAssignmentTarget"
                groupId       = $assignment.target.groupId
            }
        }
        elseif ($originalTargetType -match 'allLicensedUsersAssignmentTarget') {
            $targetObject = @{
                "@odata.type" = "#microsoft.graph.allLicensedUsersAssignmentTarget"
            }
        }
        elseif ($originalTargetType -match 'allDevicesAssignmentTarget') {
            $targetObject = @{
                "@odata.type" = "#microsoft.graph.allDevicesAssignmentTarget"
            }
        }
        else {
            Write-Log "⚠️ Warning: Unsupported assignment target type '$originalTargetType' found. Skipping this assignment." -Type "Warning"
            continue # Skip to the next assignment
        }

        # Build the main assignment body
        $assignmentBody = @{
            "@odata.type" = "#microsoft.graph.mobileAppAssignment" # Explicitly set the assignment type
            target        = $targetObject # Use the constructed target object
        }

        # Add intent (mandatory)
        $assignmentBody.intent = $assignment.intent

        # Conditionally add optional settings if they exist in the source assignment
        if ($assignment.PSObject.Properties.Name -contains 'settings' -and $assignment.settings -ne $null) {
            $assignmentBody.settings = $assignment.settings
        }
        # 'source' is usually determined by Intune and not needed for POST
        # 'sourceId' is read-only and should not be included

        $assignmentJson = $assignmentBody | ConvertTo-Json -Depth 5 -Compress

        try {
            $targetDescription = if ($assignment.target.groupId) { "group ID: $($assignment.target.groupId)" } elseif ($assignment.target.'@odata.type') { $assignment.target.'@odata.type' } else { "unknown target" }
            Write-Log "   • Applying assignment for target $targetDescription" -Type "Info"
            # Use Invoke-MgGraphRequest for consistency
            Invoke-MgGraphRequest -Method POST -Uri $assignmentsUri -Body $assignmentJson -ErrorAction Stop | Out-Null
            $appliedCount++
        }
        catch {
            $failedCount++
            Write-Log "❌ Error applying assignment for target $targetDescription : $_" -Type "Error"
            # Log the failed assignment body for debugging if needed
            # Write-Host "Failed assignment body: $assignmentJson" -ForegroundColor DarkGray
        }
    }
    
    Write-Log "---------------------------------------------------" -Type "Info"
    if ($appliedCount -gt 0) {
        Write-Log "✅ Successfully applied $appliedCount assignment(s)." -Type "Info"
    }
    if ($failedCount -gt 0) {
        Write-Log "❌ Failed to apply $failedCount assignment(s)." -Type "Error"
    }
    # (Function definition removed from here)


    if ($appliedCount -eq 0 -and $failedCount -eq 0) {
        Write-Log "ℹ️ No assignments were processed." -Type "Info" # Should not happen if $Assignments was not empty initially
    }
    Write-Log "---------------------------------------------------" -Type "Info"
}

# Function to remove assignments from a specific Intune app
function Remove-IntuneAppAssignments {
    param (
        [string]$OldAppId,
        [array]$AssignmentsToRemove
    )

    if ([string]::IsNullOrEmpty($OldAppId)) {
        Write-Log "Error: Old App ID is required to remove assignments." -Type "Error"
        return
    }

    if ($AssignmentsToRemove -eq $null -or $AssignmentsToRemove.Count -eq 0) {
        Write-Log "ℹ️ No assignments specified for removal." -Type "Info"
        return
    }

    Write-Log "Removing assignments from old app (ID: $OldAppId)..." -Type "Info"
    $removedCount = 0
    $failedCount = 0

    foreach ($assignment in $AssignmentsToRemove) {
        # Each assignment fetched earlier has its own ID
        $assignmentId = $assignment.id
        if ([string]::IsNullOrEmpty($assignmentId)) {
            Write-Log "⚠️ Warning: Assignment found without an ID. Cannot remove." -Type "Warning"
            continue
        }

        $removeUri = "https://graph.microsoft.com/beta/deviceAppManagement/mobileApps/$OldAppId/assignments/$assignmentId"
    
        # Determine target description for logging
        $targetDescription = "assignment ID: $assignmentId"
        if ($assignment.target.groupId) { $targetDescription = "group ID: $($assignment.target.groupId)" }
        elseif ($assignment.target.'@odata.type' -match 'allLicensedUsersAssignmentTarget') { $targetDescription = "All Users" }
        elseif ($assignment.target.'@odata.type' -match 'allDevicesAssignmentTarget') { $targetDescription = "All Devices" }

        try {
            Write-Log "   • Removing assignment for target $targetDescription" -Type "Info"
            Invoke-MgGraphRequest -Method DELETE -Uri $removeUri -ErrorAction Stop | Out-Null
            $removedCount++
        }
        catch {
            $failedCount++
            Write-Log "❌ Error removing assignment for target $targetDescription : $_" -Type "Error"
        }
    }

    Write-Log "---------------------------------------------------" -Type "Info"
    if ($removedCount -gt 0) {
        Write-Log "✅ Successfully removed $removedCount assignment(s) from old app." -Type "Info"
    }
    if ($failedCount -gt 0) {
        Write-Log "❌ Failed to remove $failedCount assignment(s) from old app." -Type "Error"
    }
    if ($removedCount -eq 0 -and $failedCount -eq 0) {
        Write-Log "ℹ️ No assignments were processed for removal." -Type "Info"
    }
    Write-Log "---------------------------------------------------" -Type "Info"
}

function Add-IntuneAppLogo {
    param (
        [string]$appId,
        [string]$appName,
        [string]$appType,
        [string]$localLogoPath = $null
    )

    Write-Log "Adding app logo..." -Type "Info"
    
    try {
        $tempLogoPath = $null

        if ($localLogoPath -and (Test-Path $localLogoPath)) {
            # Use the provided local logo file
            $tempLogoPath = $localLogoPath
            Write-Log "Using local logo file: $localLogoPath" -Type "Info"
        }
        else {
            # Try to download from repository
            $logoFileName = $appName.ToLower().Replace(" ", "_") + ".png"
            $logoUrl = "https://raw.githubusercontent.com/RobinMJD/IntuneBrew/$script:IntuneBrewCatalogCommit/Logos/$logoFileName"
            Write-Log "Downloading logo from: $logoUrl" -Type "Info"
            
            # Download the logo
            $tempLogoPath = Join-Path $PWD "temp_logo.png"
            try {
                Invoke-WebRequest -Uri $logoUrl -OutFile $tempLogoPath
            }
            catch {
                Write-Log "⚠️ Could not download logo from repository. Error: $_" -Type "Warning"
                return
            }
        }

        if (-not $tempLogoPath -or -not (Test-Path $tempLogoPath)) {
            Write-Log "⚠️ No valid logo file available" -Type "Warning"
            return
        }

        # Convert the logo to base64
        $logoContent = [System.Convert]::ToBase64String([System.IO.File]::ReadAllBytes($tempLogoPath))

        # Prepare the request body
        $logoBody = @{
            "@odata.type" = "#microsoft.graph.mimeContent"
            "type"        = "image/png"
            "value"       = $logoContent
        }

        # Update the app with the logo
        $logoUri = "https://graph.microsoft.com/beta/deviceAppManagement/mobileApps/$appId"
        $updateBody = @{
            "@odata.type" = "#microsoft.graph.$appType"
            "largeIcon"   = $logoBody
        }

        Invoke-MgGraphRequest -Method PATCH -Uri $logoUri -Body ($updateBody | ConvertTo-Json -Depth 10)
        Write-Log "✅ Logo added successfully" -Type "Info"

        # Cleanup
        if (Test-Path $tempLogoPath) {
            Remove-Item $tempLogoPath -Force
        }
    }
    catch {
        Write-Log "⚠️ Warning: Could not add app logo. Error: $_" -Type "Warning"
    }
}

# Enforce catalog freshness and use one immutable repository snapshot for the full run.
$githubJsonUrls = @()
$script:CatalogManifestFailureCount = 0

try {
    $catalogState = Get-PublishedCatalogState -MaximumAgeHours ([double]$CatalogMaxAgeHours)
    if (-not $catalogState.Fresh) {
        throw "CATALOG_STALE: Published catalog state is $($catalogState.AgeHours) hours old; maximum allowed age is $CatalogMaxAgeHours hours."
    }
    if ($ExecutionMode -eq 'Canary') {
        $approvedAppGuid = [guid]::Empty
        if ($ApprovedCatalogCommit -notmatch '^[0-9a-f]{40}$' -or
            $ApprovedMarkerCommit -notmatch '^[0-9a-f]{40}$' -or
            -not [guid]::TryParse($ApprovedIntuneAppId, [ref]$approvedAppGuid)) {
            throw 'Canary mode requires valid ApprovedCatalogCommit, ApprovedMarkerCommit, and ApprovedIntuneAppId parameters.'
        }
        if ($ApprovedCatalogCommit -ne $catalogState.CatalogCommit -or
            $ApprovedMarkerCommit -ne $catalogState.MarkerCommit) {
            throw 'The published catalog state changed after canary approval. Run the read-only audit again.'
        }
    }

    $catalogCommit = $catalogState.CatalogCommit

    $script:IntuneBrewCatalogCommit = $catalogCommit
    $supportedAppsUrl = "https://raw.githubusercontent.com/RobinMJD/IntuneBrew/$catalogCommit/supported_apps.json"
    $supportedApps = Invoke-GitHubRequestWithRetry -Uri $supportedAppsUrl
    
    # Pin every manifest URL to the same commit to prevent a mixed catalog during a run.
    Write-Log "Checking existing Intune applications for available updates..." -Type "Info"
    Write-Log "Catalog snapshot commit: $catalogCommit (age: $($catalogState.AgeHours) hours, run: $($catalogState.RunId))" -Type "Info"
    $githubJsonUrls = @($supportedApps.PSObject.Properties.Value | ForEach-Object {
        ConvertTo-CommitManifestUri -Uri ([string]$_) -Commit $catalogCommit
    })
    
    if ($githubJsonUrls.Count -eq 0) {
        throw "No applications were found in the supported-app catalog."
    }
}
catch {
    Write-Log "Catalog readiness check failed: $_" -Type "Error"
    throw
}

# Core Functions

# Fetches app information from GitHub JSON file
function Get-GitHubAppInfo {
    param(
        [string]$jsonUrl
    )

    if ([string]::IsNullOrEmpty($jsonUrl)) {
        Write-Log "Error: Empty or null JSON URL provided." -Type "Verbose"
        return $null
    }

    try {
        $response = Invoke-GitHubRequestWithRetry -Uri $jsonUrl
        foreach ($requiredProperty in 'name', 'version', 'url', 'bundleId', 'fileName', 'sha') {
            if ([string]::IsNullOrWhiteSpace([string]$response.$requiredProperty)) {
                throw "The manifest is missing required '$requiredProperty' data."
            }
        }

        $packageUri = [Uri][string]$response.url
        if ($packageUri.Scheme -ne 'https' -or
            -not $packageUri.IsDefaultPort -or
            -not [string]::IsNullOrEmpty($packageUri.UserInfo) -or
            -not [string]::IsNullOrEmpty($packageUri.Query) -or
            -not [string]::IsNullOrEmpty($packageUri.Fragment)) {
            throw "The package URL is not an approved HTTPS URL: $($response.url)"
        }
        if ($packageUri.Host -eq 'intunebrew.blob.core.windows.net') {
            throw 'The manifest still references the upstream IntuneBrew package cache.'
        }
        if ($packageUri.Host -eq $script:PackageStorageBaseUri.Host -and
            -not (Test-PrivatePackageUrl -Uri ([string]$response.url))) {
            throw 'The manifest references an unexpected path in the private package storage account.'
        }
        if ([string]$response.sha -notmatch '^[0-9a-fA-F]{64}$') {
            throw 'The manifest SHA256 value is invalid.'
        }
        if ([string]$response.fileName -notmatch '\.(?:dmg|pkg)$') {
            throw "The manifest package filename is unsupported: $($response.fileName)"
        }
        if (-not (Test-SafeLeafFileName -Name ([string]$response.fileName))) {
            throw "The manifest package filename is unsafe: $($response.fileName)"
        }

        return @{
            name        = $response.name
            description = $response.description
            version     = $response.version
            url         = $response.url
            bundleId    = $response.bundleId
            homepage    = $response.homepage
            fileName    = $response.fileName
            sha         = $response.sha
            manifestUri = $jsonUrl
        }
    }
    catch {
        $script:CatalogManifestFailureCount++
        Write-Log "Catalog manifest validation failed for $jsonUrl`: $_" -Type "Warning"
        return $null
    }
}

# Downloads app installer file with progress indication
function Download-AppFile($url, $fileName, $expectedHash, [long]$expectedSize) {
    if (-not (Test-SafeLeafFileName -Name $fileName)) {
        throw "Unsafe package filename: $fileName"
    }
    if ($expectedSize -le 0) {
        throw "Invalid expected package size for $fileName`: $expectedSize"
    }
    $outputPath = Join-Path $script:WorkingDirectory $fileName
    
    # Get file size before downloading
    try {
        $fileSize = [math]::Round(($expectedSize / 1MB), 2)
        Write-Log "Downloading the app file ($fileSize MB) to $outputPath..." -Type "Verbose"
    }
    catch {
        Write-Log "Downloading the app file to $outputPath..." -Type "Verbose"
    }
    
    $ProgressPreference = 'SilentlyContinue'
    Invoke-PackageWebRequest -Uri $url -Method Get -OutFile $outputPath | Out-Null
    $actualSize = (Get-Item -LiteralPath $outputPath -ErrorAction Stop).Length
    if ($actualSize -ne $expectedSize) {
        Remove-Item -LiteralPath $outputPath -Force -ErrorAction SilentlyContinue
        throw "Downloaded package size mismatch for $fileName. Expected $expectedSize bytes, received $actualSize bytes."
    }

    Write-Log "✅ Download complete" -Type "Verbose"
    
    # Validate file integrity using SHA256 hash
    Write-Log "`n🔐 Validating file integrity..." -Type "Verbose"
    
    # Validate expected hash format
    if ([string]::IsNullOrWhiteSpace($expectedHash)) {
        Write-Log "❌ Error: No SHA256 hash provided in the app manifest" -Type "Verbose"
        Remove-Item $outputPath -Force
        throw "SHA256 hash validation failed - No hash provided in app manifest"
    }
    if ($expectedHash -notmatch '^[0-9a-fA-F]{64}$') {
        Remove-Item $outputPath -Force
        throw "SHA256 hash validation failed - Invalid hash format in app manifest"
    }
    
    Write-Log "   • Verifying the downloaded file matches the expected SHA256 hash" -Type "Verbose"
    Write-Log "   • This ensures the file hasn't been corrupted or tampered with" -Type "Verbose"
    Write-Log "   " -Type "Verbose"
    Write-Log "   • Expected hash: $expectedHash" -Type "Verbose"
    Write-Log "   • Calculating file hash..." -Type "Verbose"
    $fileHash = Get-FileHash -Path $outputPath -Algorithm SHA256
    Write-Log "   • Actual hash: $($fileHash.Hash)" -Type "Verbose"
    
    # Case-insensitive comparison of the hashes
    $expectedHashNormalized = $expectedHash.Trim().ToLower()
    $actualHashNormalized = $fileHash.Hash.Trim().ToLower()
    
    if ($actualHashNormalized -eq $expectedHashNormalized) {
        Write-Log "`n✅ Security check passed - File integrity verified" -Type "Verbose"
        Write-Log "   • The SHA256 hash of the downloaded file matches the expected value" -Type "Verbose"
        Write-Log "   • This confirms the file is authentic and hasn't been modified" -Type "Verbose"
        return $outputPath
    }
    else {
        Write-Log "`n❌ Security check failed - File integrity validation error!" -Type "Verbose"
        Remove-Item $outputPath -Force
        Write-Log "`n" -Type "Verbose"
        throw "Security validation failed - SHA256 hash of the downloaded file does not match the expected value"
    }
}

# Validates GitHub URL format for security
function Is-ValidUrl {
    param (
        [string]$url
    )

    try {
        $manifestUri = [Uri]$url
        $decodedPath = [Uri]::UnescapeDataString($manifestUri.AbsolutePath)
        $fileName = [IO.Path]::GetFileName($decodedPath)
        return $manifestUri.Scheme -eq 'https' -and
            $manifestUri.IsDefaultPort -and
            [string]::IsNullOrEmpty($manifestUri.UserInfo) -and
            $manifestUri.Host -eq 'raw.githubusercontent.com' -and
            [string]::IsNullOrEmpty($manifestUri.Query) -and
            [string]::IsNullOrEmpty($manifestUri.Fragment) -and
            (Test-SafeLeafFileName -Name $fileName) -and
            $fileName -match '\.json$' -and
            $decodedPath -match "^/RobinMJD/IntuneBrew/[0-9a-f]{40}/Apps/$([regex]::Escape($fileName))$"
    }
    catch {
        Write-Log "Invalid URL format: $url" -Type "Verbose"
        return $false
    }
}

# Retrieves and compares app versions between Intune and GitHub
# Compares two dotted version strings segment by segment. PowerShell's [Version] type
# only supports four segments, which breaks five-part versions like Corretto's
# 21.0.8.9.1 (Issue #168). Returns a negative number, zero, or a positive number.
function Compare-VersionSegments {
    param(
        [string]$VersionA,
        [string]$VersionB
    )

    $partsA = $VersionA -split '\.'
    $partsB = $VersionB -split '\.'
    $maxLength = [Math]::Max($partsA.Length, $partsB.Length)

    for ($i = 0; $i -lt $maxLength; $i++) {
        $segmentA = if ($i -lt $partsA.Length) { $partsA[$i].Trim() } else { "0" }
        $segmentB = if ($i -lt $partsB.Length) { $partsB[$i].Trim() } else { "0" }

        $numA = [int64]0
        $numB = [int64]0
        $isNumA = [int64]::TryParse($segmentA, [ref]$numA)
        $isNumB = [int64]::TryParse($segmentB, [ref]$numB)

        if ($isNumA -and $isNumB) {
            if ($numA -gt $numB) { return 1 }
            if ($numA -lt $numB) { return -1 }
        }
        else {
            if (-not [string]::Equals($segmentA, $segmentB, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Version comparison is indeterminate for nonnumeric segments '$segmentA' and '$segmentB'."
            }
        }
    }
    return 0
}

# Converts a version string into a zero-padded form so string sorting matches
# numeric version ordering regardless of segment count (Issue #168)
function Convert-VersionToSortable {
    param([string]$Version)

    $segments = ($Version -replace '-.*$' -replace '\s*\(.*\)$', '') -split '[.,]'
    $padded = foreach ($segment in $segments) {
        $num = [int64]0
        if ([int64]::TryParse($segment.Trim(), [ref]$num)) { $num.ToString("D12") } else { $segment }
    }
    return ($padded -join '.')
}

# Cache of all macOS PKG/DMG apps in the tenant, used to match apps by bundle id
# when the display name lookup finds nothing (e.g. renamed apps, Issue #217)
$script:allMacOsAppsCache = $null
function Get-AllMacOsApps {
    if ($null -ne $script:allMacOsAppsCache) {
        return $script:allMacOsAppsCache
    }

    $allApps = @()
    $uri = "https://graph.microsoft.com/beta/deviceAppManagement/mobileApps?`$filter=(isof('microsoft.graph.macOSDmgApp') or isof('microsoft.graph.macOSPkgApp'))"
    while ($uri) {
        $page = Invoke-MgGraphRequest -Uri $uri -Method Get -ErrorAction Stop
        if ($null -eq $page -or $page.PSObject.Properties.Name -notcontains 'value') {
            throw "Microsoft Graph returned an invalid mobile-app page for $uri"
        }
        foreach ($app in @($page.value)) {
            if ([string]::IsNullOrWhiteSpace([string]$app.id) -or
                [string]::IsNullOrWhiteSpace([string]$app.displayName) -or
                [string]::IsNullOrWhiteSpace([string]$app.primaryBundleId) -or
                [string]::IsNullOrWhiteSpace([string]$app.'@odata.type')) {
                throw 'Microsoft Graph returned an incomplete macOS app record.'
            }
            $allApps += $app
        }
        $uri = [string]$page.'@odata.nextLink'
    }
    $script:allMacOsAppsCache = $allApps
    return $allApps
}

function Test-CompatibleIntuneDisplayName {
    param(
        [string]$CatalogName,
        [string]$IntuneDisplayName
    )

    if ([string]::IsNullOrWhiteSpace($CatalogName) -or [string]::IsNullOrWhiteSpace($IntuneDisplayName)) {
        return $false
    }

    # Allow organizational prefixes such as "[CA-SON] " but reject unrelated renamed apps.
    $normalizedDisplayName = $IntuneDisplayName.Trim() -replace "^(?:\[[^\]]+\]\s*)+", ""
    return [string]::Equals($normalizedDisplayName, $CatalogName.Trim(), [StringComparison]::OrdinalIgnoreCase)
}

function Get-ValidatedIntuneTarget {
    param([Parameter(Mandatory = $true)][object]$App)

    $appInfo = $App.CatalogInfo
    $expectedODataType = if ([string]$appInfo.fileName -match '\.dmg$') {
        '#microsoft.graph.macOSDmgApp'
    }
    else {
        '#microsoft.graph.macOSPkgApp'
    }
    $uri = "https://graph.microsoft.com/beta/deviceAppManagement/mobileApps/$($App.IntuneAppId)"
    $responseHeaders = $null
    $target = Invoke-MgGraphRequest -Uri $uri -Method Get -ErrorAction Stop `
        -ResponseHeadersVariable responseHeaders
    if ($null -eq $target -or
        -not [string]::Equals([string]$target.id, [string]$App.IntuneAppId, [StringComparison]::OrdinalIgnoreCase) -or
        -not [string]::Equals([string]$target.primaryBundleId, [string]$appInfo.bundleId, [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-CompatibleIntuneDisplayName -CatalogName ([string]$appInfo.name) -IntuneDisplayName ([string]$target.displayName)) -or
        -not [string]::Equals([string]$target.'@odata.type', $expectedODataType, [StringComparison]::OrdinalIgnoreCase) -or
        -not [string]::Equals([string]$target.primaryBundleVersion, [string]$App.IntuneVersion, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The Intune target changed after preflight for $($App.Name)."
    }
    $invalidIncludedApps = @(@($target.includedApps) | Where-Object {
        [string]::IsNullOrWhiteSpace([string]$_.bundleId) -or
        [string]::IsNullOrWhiteSpace([string]$_.bundleVersion)
    })
    if ($invalidIncludedApps.Count -gt 0) {
        throw "The Intune target has incomplete included-app data for $($App.Name)."
    }
    $targetEtag = [string]$target.'@odata.etag'
    if ([string]::IsNullOrWhiteSpace($targetEtag)) {
        $targetEtag = [string]$responseHeaders.ETag
    }
    if (-not [string]::IsNullOrWhiteSpace($targetEtag)) {
        $target | Add-Member -NotePropertyName '@odata.etag' -NotePropertyValue $targetEtag -Force
    }

    $target
}

function Get-IntuneApps {
    $intuneApps = @()
    $allMacOsApps = @(Get-AllMacOsApps)
    $totalApps = $script:CatalogEntries.Count
    $currentApp = 0
    $matchedIntuneAppIds = @{}

    Write-Log "Checking app versions in Intune..."

    foreach ($catalogEntry in $script:CatalogEntries) {
        $currentApp++
        $appInfo = $catalogEntry.Info
        $appName = $appInfo.name
        $expectedODataType = if ([string]$appInfo.fileName -match '\.dmg$') {
            '#microsoft.graph.macOSDmgApp'
        }
        else {
            '#microsoft.graph.macOSPkgApp'
        }
        Write-Log "[$currentApp/$totalApps] Checking: $appName"

        $matches = @($allMacOsApps | Where-Object {
            [string]::Equals(([string]$_.primaryBundleId).Trim(), ([string]$appInfo.bundleId).Trim(), [StringComparison]::OrdinalIgnoreCase) -and
            (Test-CompatibleIntuneDisplayName -CatalogName $appName -IntuneDisplayName ([string]$_.displayName)) -and
            [string]::Equals([string]$_.'@odata.type', $expectedODataType, [StringComparison]::OrdinalIgnoreCase)
        })

        if ($matches.Count -gt 1) {
            $script:IntunePreflightFailureCount++
            Write-Log "AMBIGUOUS_INTUNE_MATCH: '$appName' matched $($matches.Count) Intune apps by normalized name and bundle ID '$($appInfo.bundleId)'." -Type "Error"
            continue
        }

        if ($matches.Count -eq 0) {
            $partialMatches = @($allMacOsApps | Where-Object {
                [string]::Equals(([string]$_.primaryBundleId).Trim(), ([string]$appInfo.bundleId).Trim(), [StringComparison]::OrdinalIgnoreCase) -or
                (Test-CompatibleIntuneDisplayName -CatalogName $appName -IntuneDisplayName ([string]$_.displayName))
            })
            if ($partialMatches.Count -gt 0) {
                Write-Log "UNSAFE_MATCH_SKIPPED: '$appName' has only partial name or bundle-ID matches in Intune." -Type "Warning"
            }
            $intuneApps += [PSCustomObject]@{
                Name          = $appName
                IntuneVersion = 'Not in Intune'
                IntuneAppId   = $null
                GitHubVersion = $appInfo.version
                CatalogInfo   = $appInfo
                ManifestUri   = $catalogEntry.ManifestUri
            }
            continue
        }

        $matchedApp = $matches[0]
        $intuneAppId = [string]$matchedApp.id
        if ([string]::IsNullOrWhiteSpace($intuneAppId)) {
            $script:IntunePreflightFailureCount++
            Write-Log "INVALID_INTUNE_APP_ID: '$appName' matched an Intune record without an ID." -Type "Error"
            continue
        }
        if ($matchedIntuneAppIds.ContainsKey($intuneAppId)) {
            $script:IntunePreflightFailureCount++
            Write-Log "AMBIGUOUS_CATALOG_MATCH: '$appName' and '$($matchedIntuneAppIds[$intuneAppId])' map to Intune app ID $intuneAppId." -Type "Error"
            continue
        }
        $matchedIntuneAppIds[$intuneAppId] = $appName

        $intuneVersion = [string]$matchedApp.primaryBundleVersion
        if ([string]::IsNullOrWhiteSpace($intuneVersion)) {
            $script:IntunePreflightFailureCount++
            Write-Log "INVALID_INTUNE_VERSION: '$appName' has an empty primaryBundleVersion." -Type "Error"
            continue
        }
        $existingIncludedApps = @($matchedApp.includedApps)
        $invalidIncludedApps = @($existingIncludedApps | Where-Object {
            [string]::IsNullOrWhiteSpace([string]$_.bundleId) -or
            [string]::IsNullOrWhiteSpace([string]$_.bundleVersion)
        })
        if ($invalidIncludedApps.Count -gt 0) {
            $script:IntunePreflightFailureCount++
            Write-Log "INVALID_INCLUDED_APPS: '$appName' has $($invalidIncludedApps.Count) incomplete included-app record(s)." -Type "Error"
            continue
        }

        if (Is-NewerVersion $appInfo.version $intuneVersion) {
            Write-Log "Update available for $appName ($intuneVersion → $($appInfo.version))"
        }
        else {
            Write-Log "$appName is up to date (Version: $intuneVersion)"
        }

        $intuneApps += [PSCustomObject]@{
            Name          = $appName
            IntuneVersion = $intuneVersion
            IntuneAppId   = $intuneAppId
            GitHubVersion = $appInfo.version
            CatalogInfo   = $appInfo
            ManifestUri   = $catalogEntry.ManifestUri
            ExistingIncludedApps = $existingIncludedApps
        }
    }

    return $intuneApps
}

# Compares version strings accounting for build numbers
function Is-NewerVersion($githubVersion, $intuneVersion) {
    if ($intuneVersion -eq 'Not in Intune') {
        return $true
    }

    # Apps managed through Apple VPP are never updated by IntuneBrew (Issue #204)
    if ($intuneVersion -eq 'Managed via VPP') {
        return $false
    }

    try {
        # Remove hyphens and everything after them for comparison
        $ghVersion = $githubVersion -replace '-.*$'
        $itVersion = $intuneVersion -replace '-.*$'

        # Handle versions with commas (e.g., "3.5.1,16101")
        $ghVersionParts = $ghVersion -split ','
        $itVersionParts = $itVersion -split ','

        # Compare main version numbers first (strip parenthetical content like "build 6300").
        # Segment-wise comparison supports versions with five or more parts (Issue #168).
        $mainComparison = Compare-VersionSegments ($ghVersionParts[0] -replace '\s*\(.*\)$', '') ($itVersionParts[0] -replace '\s*\(.*\)$', '')

        if ($mainComparison -ne 0) {
            return ($mainComparison -gt 0)
        }

        # If main versions are equal and there are build numbers
        if ($ghVersionParts.Length -gt 1 -and $itVersionParts.Length -gt 1) {
            $ghBuild = [int64]0
            $itBuild = [int64]0
            if ([int64]::TryParse($ghVersionParts[1].Trim(), [ref]$ghBuild) -and
                [int64]::TryParse($itVersionParts[1].Trim(), [ref]$itBuild)) {
                return $ghBuild -gt $itBuild
            }
            return $false
        }

        # Normalized versions are equal. Text-only formatting differences are
        # not evidence that the catalog version is newer.
        return $false
    }
    catch {
        # silence spammy log message for not installed apps
        if ($githubVersion -eq $intuneVersion -and -not [string]::IsNullOrEmpty($githubVersion)) {
            Write-Log "Version comparison failed: GitHubVersion='$githubVersion', IntuneVersion='$intuneVersion'. Assuming versions are equal." -Type "Verbose"
        }
        return $false
    }
}

# Fetch and validate the complete catalog once before any Intune mutation.
$script:CatalogEntries = [System.Collections.Generic.List[object]]::new()
$catalogIdentityKeys = @{}
foreach ($jsonUrl in $githubJsonUrls) {
    if (-not (Is-ValidUrl -url $jsonUrl)) {
        $script:CatalogManifestFailureCount++
        Write-Log "Catalog manifest URL is invalid: $jsonUrl" -Type "Error"
        continue
    }

    $appInfo = Get-GitHubAppInfo -jsonUrl $jsonUrl
    if ($null -eq $appInfo) {
        continue
    }

    $identityKey = "$(([string]$appInfo.name).Trim().ToLowerInvariant())`0$(([string]$appInfo.bundleId).Trim().ToLowerInvariant())"
    if ($catalogIdentityKeys.ContainsKey($identityKey)) {
        $script:CatalogManifestFailureCount++
        Write-Log "Duplicate catalog identity for '$($appInfo.name)' and bundle ID '$($appInfo.bundleId)'." -Type "Error"
        continue
    }
    $catalogIdentityKeys[$identityKey] = $jsonUrl
    $script:CatalogEntries.Add([pscustomobject]@{
        ManifestUri = $jsonUrl
        Info        = $appInfo
    })
}

if ($script:CatalogManifestFailureCount -gt 0 -or
    $script:CatalogEntries.Count -eq 0 -or
    $script:CatalogEntries.Count -ne $githubJsonUrls.Count) {
    Disconnect-MgGraph > $null 2>&1
    throw "Catalog preflight failed: $($script:CatalogManifestFailureCount) invalid manifest(s), $($script:CatalogEntries.Count) of $($githubJsonUrls.Count) loaded."
}

# Retrieve Intune app versions from one complete Graph snapshot.
$script:IntunePreflightFailureCount = 0
Write-Log "Fetching current Intune app versions..."
$intuneAppVersions = Get-IntuneApps
if ($script:IntunePreflightFailureCount -gt 0) {
    Disconnect-MgGraph > $null 2>&1
    throw "Intune matching preflight failed for $($script:IntunePreflightFailureCount) catalog item(s). No Intune updates were started."
}

# Show the overview table using Write-Log
Write-Log "----------------------------------------"
Write-Log "Available Updates Overview:"
Write-Log "----------------------------------------"

$updatesAvailable = @($intuneAppVersions | Where-Object {
    $_.IntuneVersion -ne 'Not in Intune' -and (Is-NewerVersion $_.GitHubVersion $_.IntuneVersion)
})
$appsToUpload = $updatesAvailable
if ($ExecutionMode -eq 'Canary') {
    $appsToUpload = @($appsToUpload | Where-Object {
        [string]::Equals([string]$_.IntuneAppId, $ApprovedIntuneAppId, [StringComparison]::OrdinalIgnoreCase)
    })
    if ($appsToUpload.Count -ne 1) {
        throw "Canary approval did not resolve to exactly one current update candidate for Intune app ID $ApprovedIntuneAppId."
    }
}
else {
    $appsToUpload = @($appsToUpload | Sort-Object Name)
}

if ($updatesAvailable.Count -eq 0) {
    Write-Log "No updates available for any installed applications."
    Write-Log "Exiting..."
    Disconnect-MgGraph > $null 2>&1
    exit 0
}
else {
    # Create table header
    Write-Log "+--------------------------+--------------------+--------------------+"
    Write-Log "| App Name                | Current Version    | Available Version  |"
    Write-Log "+--------------------------+--------------------+--------------------+"
    
    # Add table rows
    foreach ($app in $updatesAvailable) {
        $appName = $app.Name.PadRight(24)[0..23] -join ''
        $currentVersion = $app.IntuneVersion.PadRight(18)[0..17] -join ''
        $availableVersion = $app.GitHubVersion.PadRight(18)[0..17] -join ''
        Write-Log "| $appName | $currentVersion | $availableVersion |"
        Write-Log "+--------------------------+--------------------+--------------------+"
    }
    
    Write-Log "Found $($updatesAvailable.Count) update$(if($updatesAvailable.Count -ne 1){'s'}) available."
    Write-Log "Starting update process in 10 seconds..."
    Start-Sleep -Seconds 10
}

if ($appsToUpload.Count -eq 0) {
    Write-Log "`nAll apps are up-to-date. No uploads necessary." -Type "Info"
    Disconnect-MgGraph > $null 2>&1
    Write-Log "Disconnected from Microsoft Graph." -Type "Info"
    exit 0
}

# Limit apps per run to prevent memory exhaustion in Azure Automation sandbox (Issue #45)
if ($ExecutionMode -eq 'Scheduled' -and $appsToUpload.Count -gt $MaxAppsPerRun) {
    Write-Log "Limiting to $MaxAppsPerRun apps per run (out of $($appsToUpload.Count) available) to prevent memory issues." -Type "Warning"
    Write-Log "Remaining apps will be processed in next scheduled run." -Type "Warning"
    $appsToUpload = $appsToUpload | Select-Object -First $MaxAppsPerRun
}

# Check if there are apps to process
if (($appsToUpload.Count) -eq 0) {
    Write-Log "`nNo new or updatable apps found. Exiting..." -Type "Info"
    Disconnect-MgGraph > $null 2>&1
    Write-Log "Disconnected from Microsoft Graph." -Type "Info"
    exit 0
}

# Determine if assignments should be copied based on the -CopyAssignments switch
$copyAssignments = $CopyAssignments -eq $true

# Define variables needed for assignment checking/copying regardless of mode
$updatableApps = @($appsToUpload | Where-Object { $_.IntuneVersion -ne 'Not in Intune' -and (Is-NewerVersion $_.GitHubVersion $_.IntuneVersion) })
$fetchedAssignments = @{} # Hashtable to store fetched assignments [AppID -> AssignmentsArray]
$assignmentsFound = $false # Flag to track if any assignments were found

# --- Non-Interactive Assignment Check/Display ---
# Pre-fetch and display assignments if running non-interactively (-Upload or -UpdateAll) AND copying is requested (-CopyAssignments) AND updates exist
if ($copyAssignments -and $updatableApps.Length -gt 0) {
    Write-Log "`nChecking assignments for apps to be updated..." -Type "Info"
    foreach ($updApp in $updatableApps) {
        $assignments = Get-IntuneAppAssignments -AppId $updApp.IntuneAppId
        if ($assignments -ne $null -and $assignments.Count -gt 0) {
            $fetchedAssignments[$updApp.IntuneAppId] = $assignments
            # $assignmentsFound = $true # Not needed for non-interactive prompt logic
            # Display summary for this app
            $assignmentSummaries = @()
            foreach ($assignment in $assignments) {
                $rawTargetType = $assignment.target.'@odata.type'.Replace("#microsoft.graph.", "")
                $groupId = $assignment.target.groupId
                $displayType = ""
                $targetDetail = ""
                switch ($rawTargetType) {
                    "groupAssignmentTarget" {
                        $displayType = "Group"
                        if ($groupId) {
                            try {
                                $groupUri = "https://graph.microsoft.com/v1.0/groups/$groupId`?`$select=displayName"
                                $groupInfo = Invoke-MgGraphRequest -Method GET -Uri $groupUri
                                if ($groupInfo.displayName) { $targetDetail = "('$($groupInfo.displayName)')" }
                                else { $targetDetail = "(ID: $groupId)" }
                            }
                            catch {
                                Write-Log "⚠️ Warning: Could not fetch display name for Group ID $groupId. Error: $($_.Exception.Message)" -Type "Warning"
                                $targetDetail = "(ID: $groupId)"
                            }
                        }
                        else { $targetDetail = "(Unknown Group ID)" }
                    }
                    "allLicensedUsersAssignmentTarget" { $displayType = "All Users" }
                    "allDevicesAssignmentTarget" { $displayType = "All Devices" }
                    default { $displayType = $rawTargetType }
                }
                $summaryPart = "$($assignment.intent): $displayType"
                if (-not [string]::IsNullOrWhiteSpace($targetDetail)) { $summaryPart += " $targetDetail" }
                $assignmentSummaries += $summaryPart
            }
            Write-Log "  - $($updApp.Name): Found $($assignments.Count) assignment(s): $($assignmentSummaries -join ', ')" -Type "Info"
        }
        else {
            Write-Log "  - $($updApp.Name): No assignments found." -Type "Info"
        }
    }
    Write-Log "   " -Type "Info"
}

$existingAssignments = $null # Initialize variable to store assignments for updates

# Main script for uploading only newer apps
$script:WorkingDirectory = Join-Path $env:TEMP "IntuneBrew-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $script:WorkingDirectory -ErrorAction Stop | Out-Null
$processingFailureCount = 0
foreach ($app in $appsToUpload) {
    try {
        # Clear memory before processing each app to prevent Azure sandbox suspension (Issue #45)
        Clear-MemoryAggressively

        Write-Log "Processing application: $($app.Name)"
        Write-Log "Current version in Intune: $($app.IntuneVersion)"
        Write-Log "Available version: $($app.GitHubVersion)"
        
        $appInfo = $app.CatalogInfo
        if ($null -eq $appInfo) {
            throw "Immutable catalog data is missing for $($app.Name)."
        }

        # Check if this is an update and fetch existing assignments
        $existingAssignments = $null # Reset for each app
        # Fetch assignments only if the flag is set and it's an update
        # Retrieve pre-fetched assignments if the flag is set and it's an update
        if ($copyAssignments -and $app.IntuneAppId -and $fetchedAssignments.ContainsKey($app.IntuneAppId)) {
            $existingAssignments = $fetchedAssignments[$app.IntuneAppId]
        }

        # Clean up any existing temporary files before starting new download
        Get-ChildItem -LiteralPath $script:WorkingDirectory -File | ForEach-Object {
            try {
                Remove-Item $_.FullName -Force -ErrorAction Stop
                Write-Log "Cleaned up temporary file: $($_.Name)"
            }
            catch {
                Write-Log "Warning: Could not remove temporary file $($_.Name): $_" -Type "Warning"
            }
        }

        # Force garbage collection before starting new app
        [System.GC]::Collect()
        [System.GC]::WaitForPendingFinalizers()

        Write-Log "Starting upload process for: $($appInfo.name)"
        Write-Log "Downloading application from: $($appInfo.url)"
        
        # Check available space before downloading
        $drive = Get-PSDrive -Name (Get-Item -LiteralPath $script:WorkingDirectory).PSDrive.Name
        $availableSpace = $drive.Free
        $requiredSpace = 0
        
        try {
            $response = Invoke-PackageWebRequest -Uri $appInfo.url -Method Head
            $fileSize = [long]$response.Headers.'Content-Length'
            if ($fileSize -le 0) {
                throw 'The package server did not return a positive Content-Length.'
            }
            # We need space for both the original and encrypted file, plus some buffer
            $requiredSpace = $fileSize * 2.5
        }
        catch {
            throw "Could not determine package size for $($appInfo.name): $_"
        }

        if ($availableSpace -lt $requiredSpace) {
            throw "Not enough space to process $($appInfo.name). Required: $([math]::Round($requiredSpace/1GB, 2))GB, Available: $([math]::Round($availableSpace/1GB, 2))GB"
        }

        $appFilePath = Download-AppFile $appInfo.url $appInfo.fileName $appInfo.sha $fileSize

        Write-Log "Application Details:"
        Write-Log "• Display Name: $($appInfo.name)"
        Write-Log "• Version: $($appInfo.version)"
        Write-Log "• Bundle ID: $($appInfo.bundleId)"
        Write-Log "• File: $(Split-Path $appFilePath -Leaf)"

        $appDisplayName = $appInfo.name
        $appDescription = $appInfo.description
        $appPublisher = $appInfo.name
        $appHomepage = $appInfo.homepage
        $appBundleId = $appInfo.bundleId
        $appBundleVersion = $appInfo.version

        # Determine app type based on file extension
        $appType = if ($appInfo.fileName -match '\.dmg$') {
            "macOSDmgApp"
        }
        elseif ($appInfo.fileName -match '\.pkg$') {
            "macOSPkgApp"
        }
        else {
            throw "Unsupported file type for $($appInfo.name). Only .dmg and .pkg files are supported."
        }

        if ([string]::IsNullOrWhiteSpace([string]$app.IntuneAppId)) {
            throw "Update-only safety check failed: $($app.Name) has no unique existing Intune app ID."
        }
        $intuneAppId = [string]$app.IntuneAppId
        Get-ValidatedIntuneTarget -App $app | Out-Null
        Write-Log "🔄 Updating Existing Intune App (ID: $intuneAppId)"
        Write-Log "Note: Existing app settings (assignments, logo, etc.) will be preserved"

        Write-Log "🔒 Processing content version..."
        $contentVersionUri = "https://graph.microsoft.com/beta/deviceAppManagement/mobileApps/$($intuneAppId)/microsoft.graph.$appType/contentVersions"
        $contentVersion = Invoke-MgGraphRequest -Method POST -Uri $contentVersionUri -Body "{}"
        Write-Log "Content version created (ID: $($contentVersion.id))"

        Write-Log "🔐 Encrypting application file..."
        $encryptedFilePath = "$appFilePath.bin"
        if (Test-Path $encryptedFilePath) {
            Remove-Item $encryptedFilePath -Force
        }

        # Store original file size before encryption (needed later for content file entry)
        $originalFileSize = (Get-Item $appFilePath).Length
        $originalFileName = [System.IO.Path]::GetFileName($appFilePath)

        $fileEncryptionInfo = EncryptFile $appFilePath
        Write-Log "File encryption complete"

        # Store encrypted file size
        $encryptedFileSize = (Get-Item "$appFilePath.bin").Length

        # Delete original file immediately after encryption to free disk space and memory (Issue #45)
        # The encrypted .bin file is all we need for upload
        if (Test-Path $appFilePath) {
            try {
                Remove-Item $appFilePath -Force -ErrorAction Stop
                Write-Log "Original file removed to free resources"
            }
            catch {
                Write-Log "Warning: Could not remove original file immediately: $_" -Type "Warning"
            }
        }
        Clear-MemoryAggressively

        try {
            Write-Log "⬆️ Uploading to Azure Storage..."
            $fileContent = @{
                "@odata.type" = "#microsoft.graph.mobileAppContentFile"
                name          = $originalFileName
                size          = $originalFileSize
                sizeEncrypted = $encryptedFileSize
                isDependency  = $false
            }

            Write-Log "Creating content file entry in Intune..."
            $contentFileUri = "https://graph.microsoft.com/beta/deviceAppManagement/mobileApps/$($intuneAppId)/microsoft.graph.$appType/contentVersions/$($contentVersion.id)/files"  
            $contentFile = Invoke-MgGraphRequest -Method POST -Uri $contentFileUri -Body ($fileContent | ConvertTo-Json)
            Write-Log "Content file entry created successfully"

            Write-Log "Waiting for Azure Storage URI..."
            $maxWaitAttempts = 12  # 1 minute total (5 seconds * 12)
            $waitAttempt = 0
            do {
                Start-Sleep -Seconds 5
                $waitAttempt++
                Write-Log "Checking upload state (attempt $waitAttempt of $maxWaitAttempts)..."
                
                $fileStatusUri = "https://graph.microsoft.com/beta/deviceAppManagement/mobileApps/$($intuneAppId)/microsoft.graph.$appType/contentVersions/$($contentVersion.id)/files/$($contentFile.id)"
                $fileStatus = Invoke-MgGraphRequest -Method GET -Uri $fileStatusUri
                
                if ($waitAttempt -eq $maxWaitAttempts -and $fileStatus.uploadState -ne "azureStorageUriRequestSuccess") {
                    throw "Timed out waiting for Azure Storage URI"
                }
            } while ($fileStatus.uploadState -ne "azureStorageUriRequestSuccess")

            Write-Log "Received Azure Storage URI, starting upload..."
            UploadFileToAzureStorage $fileStatus.azureStorageUri "$appFilePath.bin"
            Write-Log "Upload to Azure Storage complete"
        }
        catch {
            Write-Log "Failed during upload process: $_" -Type "Error"
            throw
        }

        Write-Log "🔄 Committing file to Intune..."
        $commitData = @{
            fileEncryptionInfo = $fileEncryptionInfo
        }
        $commitUri = "https://graph.microsoft.com/beta/deviceAppManagement/mobileApps/$($intuneAppId)/microsoft.graph.$appType/contentVersions/$($contentVersion.id)/files/$($contentFile.id)/commit"
        Invoke-MgGraphRequest -Method POST -Uri $commitUri -Body ($commitData | ConvertTo-Json)

        $commitRetryCount = 0
        $maxCommitRetries = 10
        $pollAttempt = 0
        $maxPollAttempts = 60
        do {
            Start-Sleep -Seconds 10
            $pollAttempt++
            $fileStatusUri = "https://graph.microsoft.com/beta/deviceAppManagement/mobileApps/$($intuneAppId)/microsoft.graph.$appType/contentVersions/$($contentVersion.id)/files/$($contentFile.id)"
            $fileStatus = Invoke-MgGraphRequest -Method GET -Uri $fileStatusUri
            if ($fileStatus.uploadState -eq "commitFileFailed") {
                if ($commitRetryCount -ge $maxCommitRetries) {
                    break
                }
                Invoke-MgGraphRequest -Method POST -Uri $commitUri -Body ($commitData | ConvertTo-Json) | Out-Null
                $commitRetryCount++
            }
        } while ($fileStatus.uploadState -ne "commitFileSuccess" -and $pollAttempt -lt $maxPollAttempts)

        if ($fileStatus.uploadState -eq "commitFileSuccess") {
            Write-Log "✅ File committed successfully" -Type "Info"
        }
        else {
            Write-Log "Failed to commit file after $pollAttempt status checks and $commitRetryCount retries." -Type "Error"
            throw "Failed to commit file within $($maxPollAttempts * 10) seconds for $($app.Name). Last state: $($fileStatus.uploadState)"
        }

        $updateAppUri = "https://graph.microsoft.com/beta/deviceAppManagement/mobileApps/$($intuneAppId)"
        $targetBeforePatch = Get-ValidatedIntuneTarget -App $app
        $updateData = @{
            "@odata.type"           = "#microsoft.graph.$appType"
            committedContentVersion = $contentVersion.id
        }

        # The version fields must be updated together with the committed content version.
        # Without them an existing app keeps reporting the old version, so the same update
        # is re-applied on every run (Issue #216).
        if ($appType -eq "macOSDmgApp" -or $appType -eq "macOSPkgApp") {
            $updateData["versionNumber"] = $appBundleVersion
            $updateData["primaryBundleId"] = $appBundleId
            $updateData["primaryBundleVersion"] = $appBundleVersion
            $existingIncludedApps = @($targetBeforePatch.includedApps)
            if ($existingIncludedApps.Count -gt 0) {
                $updateData["includedApps"] = @($existingIncludedApps | ForEach-Object {
                    $includedBundleVersion = if ([string]::Equals(
                        [string]$_.bundleId,
                        $appBundleId,
                        [StringComparison]::OrdinalIgnoreCase
                    )) {
                        $appBundleVersion
                    }
                    else {
                        [string]$_.bundleVersion
                    }
                    @{
                    "@odata.type" = "#microsoft.graph.macOSIncludedApp"
                    bundleId      = [string]$_.bundleId
                    bundleVersion = $includedBundleVersion
                    }
                })
            }
        }

        $patchParameters = @{
            Method = 'PATCH'
            Uri    = $updateAppUri
            Body   = ($updateData | ConvertTo-Json -Depth 10)
        }
        $targetEtag = [string]$targetBeforePatch.'@odata.etag'
        if (-not [string]::IsNullOrWhiteSpace($targetEtag)) {
            $patchParameters.Headers = @{ 'If-Match' = $targetEtag }
        }
        Invoke-MgGraphRequest @patchParameters

            # Apply assignments if the flag is set and assignments were successfully fetched
        if ($copyAssignments -and $existingAssignments -ne $null) {
            Set-IntuneAppAssignments -NewAppId $intuneAppId -Assignments $existingAssignments
            # Now remove assignments from the old app version
            Remove-IntuneAppAssignments -OldAppId $app.IntuneAppId -AssignmentsToRemove $existingAssignments
        }

        Write-Log "Existing app logo and assignments were left unchanged." -Type "Info"

        Write-Log "🧹 Cleaning up temporary files..."
        if (Test-Path $appFilePath) {
            try {
                [System.GC]::Collect()
                [System.GC]::WaitForPendingFinalizers()
                Remove-Item $appFilePath -Force -ErrorAction Stop
            }
            catch {
                Write-Log "Warning: Could not remove $appFilePath. Error: $_" -Type "Warning"
            }
        }
        if (Test-Path "$appFilePath.bin") {
            $maxAttempts = 3
            $attempt = 0
            $success = $false
            
            while (-not $success -and $attempt -lt $maxAttempts) {
                try {
                    [System.GC]::Collect()
                    [System.GC]::WaitForPendingFinalizers()
                    Start-Sleep -Seconds 2  # Give processes time to release handles
                    Remove-Item "$appFilePath.bin" -Force -ErrorAction Stop
                    $success = $true
                }
                catch {
                    $attempt++
                    if ($attempt -lt $maxAttempts) {
                        Write-Log "Retry $attempt of $maxAttempts to remove encrypted file..." -Type "Warning"
                        Start-Sleep -Seconds 2
                    }
                    else {
                        Write-Log "Warning: Could not remove encrypted file. Error: $_" -Type "Warning"
                    }
                }
            }
        }
        Write-Log "✅ Cleanup complete" -Type "Info"

        Write-Log "Successfully processed $($appInfo.name)"
        Write-Log "App is now available in Intune Portal: https://intune.microsoft.com/#view/Microsoft_Intune_Apps/SettingsMenu/~/0/appId/$($intuneAppId)"
        Write-Log " " -Type "Info"
    }
    catch {
        $processingFailureCount++
        Write-Log "Critical error processing $($app.Name): $_" -Type "Error"
        Write-Log "Moving to next application..." -Type "Info"
        continue
    }
}

Write-Log "Disconnecting from Microsoft Graph"
Disconnect-MgGraph > $null 2>&1
if ($script:AzIdentityConnected) {
    Disconnect-AzAccount -Scope Process -ErrorAction SilentlyContinue | Out-Null
}
if (Test-Path -LiteralPath $script:WorkingDirectory) {
    Remove-Item -LiteralPath $script:WorkingDirectory -Recurse -Force -ErrorAction SilentlyContinue
}
if ($processingFailureCount -gt 0) {
    throw "$processingFailureCount application update(s) failed. Review the job stream before the next run."
}
Write-Log "All operations completed successfully!"
