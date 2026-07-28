#requires -Version 5.1
<#
.SYNOPSIS
Diagnose supported Kestrel paths on Windows and print an explicit bootstrap plan.

.DESCRIPTION
This script never installs Windows features, Docker Desktop, Python, Git, WSL,
or Kestrel. Doctor reports the current prerequisites. Bootstrap prints the
commands an operator may choose to execute after review.
#>

[CmdletBinding()]
param(
    [ValidateSet("Doctor", "Bootstrap")]
    [string] $Action = "Doctor",

    [ValidateSet("Auto", "NativeWheel", "WSL2", "DockerDesktop")]
    [string] $Path = "Auto",

    [ValidatePattern("^[0-9]+(?:\.[0-9]+)+(?:[A-Za-z0-9.+-]*)?$")]
    [string] $Version = "0.4.11",

    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProbeTimeoutMilliseconds = 8000

function ConvertTo-ProcessArgument {
    param([Parameter(Mandatory = $true)][string] $Value)

    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') {
        return $Value
    }
    $builder = New-Object System.Text.StringBuilder
    [void] $builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes += 1
            continue
        }
        if ($character -eq '"') {
            [void] $builder.Append(('\' * (($backslashes * 2) + 1)))
            [void] $builder.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void] $builder.Append(('\' * $backslashes))
            $backslashes = 0
        }
        [void] $builder.Append($character)
    }
    if ($backslashes -gt 0) {
        [void] $builder.Append(('\' * ($backslashes * 2)))
    }
    [void] $builder.Append('"')
    return $builder.ToString()
}

function Resolve-Executable {
    param([Parameter(Mandatory = $true)][string] $Name)

    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $command) {
        return $null
    }
    return [string] $command.Source
}

function Invoke-BoundedProbe {
    param(
        [Parameter(Mandatory = $true)][string] $Executable,
        [Parameter(Mandatory = $true)][string[]] $Arguments
    )

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $Executable
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    $argumentListProperty = $startInfo.PSObject.Properties["ArgumentList"]
    if ($null -ne $argumentListProperty) {
        foreach ($argument in $Arguments) {
            [void] $startInfo.ArgumentList.Add($argument)
        }
    }
    else {
        $startInfo.Arguments = (($Arguments | ForEach-Object {
            ConvertTo-ProcessArgument -Value $_
        }) -join " ")
    }

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        [void] $process.Start()
        if (-not $process.WaitForExit($ProbeTimeoutMilliseconds)) {
            $process.Kill()
            $process.WaitForExit()
            return [ordered] @{
                exit_code = $null
                timed_out = $true
                stdout = ""
                stderr = "probe exceeded $ProbeTimeoutMilliseconds milliseconds"
            }
        }
        $stdout = $process.StandardOutput.ReadToEnd().Trim()
        $stderr = $process.StandardError.ReadToEnd().Trim()
        return [ordered] @{
            exit_code = $process.ExitCode
            timed_out = $false
            stdout = $stdout
            stderr = $stderr
        }
    }
    catch {
        return [ordered] @{
            exit_code = $null
            timed_out = $false
            stdout = ""
            stderr = $_.Exception.Message
        }
    }
    finally {
        $process.Dispose()
    }
}

function Limit-Evidence {
    param([AllowEmptyString()][string] $Value)

    $singleLine = ($Value -replace '[\r\n]+', ' ').Trim()
    if ($singleLine.Length -le 300) {
        return $singleLine
    }
    return $singleLine.Substring(0, 300) + "..."
}

function New-Check {
    param(
        [Parameter(Mandatory = $true)][string] $Status,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string] $Evidence,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string] $Remediation
    )

    return [ordered] @{
        status = $Status
        evidence = (Limit-Evidence -Value $Evidence)
        remediation = $Remediation
    }
}

$isWindows = [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
$checks = [ordered] @{}

$gitExecutable = Resolve-Executable -Name "git.exe"
if ($null -eq $gitExecutable) {
    $checks.git = New-Check -Status "missing" `
        -Evidence "git.exe was not found on PATH" `
        -Remediation "Install Git for Windows explicitly, then open a new PowerShell session."
}
else {
    $gitProbe = Invoke-BoundedProbe -Executable $gitExecutable -Arguments @("--version")
    if ($gitProbe.exit_code -eq 0) {
        $checks.git = New-Check -Status "ready" `
            -Evidence $gitProbe.stdout `
            -Remediation ""
    }
    else {
        $checks.git = New-Check -Status "unavailable" `
            -Evidence $gitProbe.stderr `
            -Remediation "Repair Git for Windows or its PATH entry."
    }
}

$pythonCandidates = @(
    [ordered] @{ executable = "py.exe"; prefix = @("-3.13"); display = "py -3.13" },
    [ordered] @{ executable = "py.exe"; prefix = @("-3.12"); display = "py -3.12" },
    [ordered] @{ executable = "py.exe"; prefix = @("-3.11"); display = "py -3.11" },
    [ordered] @{ executable = "python.exe"; prefix = @(); display = "python" }
)
$pythonSelection = $null
foreach ($candidate in $pythonCandidates) {
    $candidateExecutable = Resolve-Executable -Name $candidate.executable
    if ($null -eq $candidateExecutable) {
        continue
    }
    $pythonArguments = @($candidate.prefix) + @(
        "-c",
        "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
    )
    $pythonProbe = Invoke-BoundedProbe -Executable $candidateExecutable -Arguments $pythonArguments
    if ($pythonProbe.exit_code -eq 0 -and $pythonProbe.stdout -match '^3\.(11|12|13)\.[0-9]+$') {
        $pythonSelection = [ordered] @{
            executable = $candidateExecutable
            prefix = @($candidate.prefix)
            display = $candidate.display
            version = $pythonProbe.stdout
        }
        break
    }
}
if ($null -eq $pythonSelection) {
    $checks.python = New-Check -Status "missing" `
        -Evidence "No supported Python 3.11, 3.12, or 3.13 interpreter was found." `
        -Remediation "Install a supported 64-bit Python release explicitly; do not use an unreviewed package-manager command."
}
else {
    $checks.python = New-Check -Status "ready" `
        -Evidence "$($pythonSelection.display) reports $($pythonSelection.version)" `
        -Remediation ""
}

$wslExecutable = Resolve-Executable -Name "wsl.exe"
$wslReady = $false
if ($null -eq $wslExecutable) {
    $checks.wsl2 = New-Check -Status "missing" `
        -Evidence "wsl.exe was not found" `
        -Remediation "Enable WSL2 explicitly in Windows Features and install an x86_64 Linux distribution."
}
else {
    $wslList = Invoke-BoundedProbe -Executable $wslExecutable -Arguments @("--list", "--verbose")
    $wslArchitecture = Invoke-BoundedProbe -Executable $wslExecutable -Arguments @(
        "--exec", "uname", "-m"
    )
    $wslPrerequisites = Invoke-BoundedProbe -Executable $wslExecutable -Arguments @(
        "--exec", "sh", "-lc", "command -v git >/dev/null && command -v python3 >/dev/null"
    )
    $hasVersionTwo = $wslList.exit_code -eq 0 -and $wslList.stdout -match '(?m)\s2\s*$'
    $hasSupportedArchitecture = (
        $wslArchitecture.exit_code -eq 0 -and
        $wslArchitecture.stdout.Trim() -eq "x86_64"
    )
    $hasGuestPrerequisites = $wslPrerequisites.exit_code -eq 0
    $wslReady = $hasVersionTwo -and $hasSupportedArchitecture -and $hasGuestPrerequisites
    if ($wslReady) {
        $checks.wsl2 = New-Check -Status "ready" `
            -Evidence "A WSL2 x86_64 distribution has git and python3." `
            -Remediation ""
    }
    else {
        $wslEvidence = @(
            "version2=$hasVersionTwo",
            "architecture=$($wslArchitecture.stdout)",
            "guest_git_python=$hasGuestPrerequisites",
            $wslList.stderr,
            $wslArchitecture.stderr,
            $wslPrerequisites.stderr
        ) -join "; "
        $checks.wsl2 = New-Check -Status "incomplete" `
            -Evidence $wslEvidence `
            -Remediation "Configure an x86_64 WSL2 distribution with git and Python 3.11-3.13; this script will not enable or install them."
    }
}

$dockerExecutable = Resolve-Executable -Name "docker.exe"
$dockerReady = $false
if ($null -eq $dockerExecutable) {
    $checks.docker_desktop = New-Check -Status "missing" `
        -Evidence "docker.exe was not found on PATH" `
        -Remediation "Install and start Docker Desktop explicitly, then enable its Linux container engine."
}
else {
    $dockerProbe = Invoke-BoundedProbe -Executable $dockerExecutable -Arguments @(
        "version", "--format", "{{.Server.Version}}"
    )
    $dockerReady = $dockerProbe.exit_code -eq 0 -and $dockerProbe.stdout.Length -gt 0
    if ($dockerReady) {
        $checks.docker_desktop = New-Check -Status "ready" `
            -Evidence "Docker engine $($dockerProbe.stdout) is reachable." `
            -Remediation ""
    }
    else {
        $checks.docker_desktop = New-Check -Status "unavailable" `
            -Evidence "$($dockerProbe.stdout) $($dockerProbe.stderr)" `
            -Remediation "Start Docker Desktop and verify the Linux container engine before retrying."
    }
}

$nativeReady = $isWindows -and $null -ne $pythonSelection
$wslPathReady = $isWindows -and $wslReady
$dockerPathReady = $isWindows -and $dockerReady
$paths = [ordered] @{
    native_wheel = [ordered] @{
        ready = $nativeReady
        requires = @("Python 3.11-3.13")
        trust_boundary = "Published exact wheel; no shell installer."
    }
    wsl2 = [ordered] @{
        ready = $wslPathReady
        requires = @("WSL2", "x86_64 Linux distribution", "git", "Python 3.11-3.13")
        trust_boundary = "The existing Bash installer runs inside WSL2, not Git Bash."
    }
    docker_desktop = [ordered] @{
        ready = $dockerPathReady
        requires = @("Docker Desktop Linux engine")
        trust_boundary = "Published Kestrel container; host prerequisites are not modified."
    }
}

$selectedPath = $null
if ($Path -eq "NativeWheel") {
    $selectedPath = "native_wheel"
}
elseif ($Path -eq "WSL2") {
    $selectedPath = "wsl2"
}
elseif ($Path -eq "DockerDesktop") {
    $selectedPath = "docker_desktop"
}
elseif ($nativeReady) {
    $selectedPath = "native_wheel"
}
elseif ($wslPathReady) {
    $selectedPath = "wsl2"
}
elseif ($dockerPathReady) {
    $selectedPath = "docker_desktop"
}

$selectedReady = $false
if ($null -ne $selectedPath) {
    $selectedReady = [bool] $paths[$selectedPath].ready
}
$commands = @()
if ($Action -eq "Bootstrap" -and $selectedReady) {
    if ($selectedPath -eq "native_wheel") {
        $pythonCommand = [string] $pythonSelection.display
        $commands = @(
            "$pythonCommand -m pip install `"nested-memvid-agent[memvid,server,mcp,keyring]==$Version`"",
            "kestrel doctor"
        )
    }
    elseif ($selectedPath -eq "wsl2") {
        $commands = @(
            "wsl.exe -- bash -lc `"curl -fsSL https://github.com/John-MiracleWorker/Kestrel/releases/download/v$Version/install.sh | bash`"",
            "wsl.exe -- kestrel doctor"
        )
    }
    elseif ($selectedPath -eq "docker_desktop") {
        $commands = @(
            "docker pull ghcr.io/john-miracleworker/kestrel:v$Version",
            "docker run --rm ghcr.io/john-miracleworker/kestrel:v$Version nest-agent doctor --backend memory --provider mock"
        )
    }
}

$report = [ordered] @{
    schema = "kestrel.windows_bootstrap_report.v1"
    platform = [ordered] @{
        native_windows = $isWindows
        powershell = $PSVersionTable.PSVersion.ToString()
    }
    action = $Action
    requested_path = $Path
    selected_path = $selectedPath
    checks = $checks
    paths = $paths
    bootstrap = [ordered] @{
        commands = $commands
        operator_execution_required = $Action -eq "Bootstrap"
        prerequisites_were_installed = $false
    }
    mutation_performed = $false
    passed = $selectedReady
}

if ($Json) {
    $report | ConvertTo-Json -Depth 8
}
else {
    Write-Output "Kestrel Windows diagnostic"
    Write-Output "  Selected path: $selectedPath"
    foreach ($name in $checks.Keys) {
        Write-Output "  $name`: $($checks[$name].status) - $($checks[$name].evidence)"
    }
    if ($Action -eq "Bootstrap" -and $commands.Count -gt 0) {
        Write-Output ""
        Write-Output "Review and run these commands yourself:"
        foreach ($command in $commands) {
            Write-Output "  $command"
        }
    }
    if (-not $selectedReady) {
        Write-Error "No requested supported path is ready. Review the remediation above."
    }
}

if ($selectedReady) {
    exit 0
}
exit 1
