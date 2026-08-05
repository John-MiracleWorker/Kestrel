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
    [string] $Version = "0.5.4",

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

    # Defense in depth: drop any null/empty elements so a caller that built the
    # array with a $null prefix can't trip "Cannot bind argument ... empty
    # string" downstream in ArgumentList/ConvertTo-ProcessArgument.
    $Arguments = @($Arguments | Where-Object { -not [string]::IsNullOrEmpty($_) })

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

$runningOnWindows = [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
$checks = [ordered] @{}

$gitReady = $false
$gitExecutable = Resolve-Executable -Name "git.exe"
if ($null -eq $gitExecutable) {
    $checks.git = New-Check -Status "missing" `
        -Evidence "git.exe was not found on PATH" `
        -Remediation "Install Git for Windows explicitly, then open a new PowerShell session."
}
else {
    $gitProbe = Invoke-BoundedProbe -Executable $gitExecutable -Arguments @("--version")
    if ($gitProbe.exit_code -eq 0) {
        $gitReady = $true
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
    # @() + $candidate.prefix can yield a leading $null when prefix is an empty
    # array (hashtable array properties surface as $null), and a $null element
    # binds to [string] as an empty string -> "Cannot bind argument ... empty
    # string". Strip null/empty entries before binding.
    $pythonArguments = @(
        @() + $candidate.prefix + @(
            "-c",
            'import struct,sys;print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}|{struct.calcsize(''P'') * 8}")'
        ) | Where-Object { -not [string]::IsNullOrEmpty($_) }
    )
    $pythonProbe = Invoke-BoundedProbe -Executable $candidateExecutable -Arguments $pythonArguments
    $pythonMatch = [regex]::Match(
        [string] $pythonProbe.stdout,
        '^(3\.(?:11|12|13)\.[0-9]+)\|(64)$'
    )
    if (-not $pythonMatch.Success) {
        continue
    }
    $pipArguments = @(
        @() + $candidate.prefix + @("-m", "pip", "--version") |
            Where-Object { -not [string]::IsNullOrEmpty($_) }
    )
    $pipProbe = Invoke-BoundedProbe -Executable $candidateExecutable -Arguments $pipArguments
    if ($pipProbe.exit_code -eq 0 -and $pipProbe.stdout -match '^pip\s+[0-9]') {
        $pythonSelection = [ordered] @{
            executable = $candidateExecutable
            prefix = @($candidate.prefix)
            display = $candidate.display
            version = $pythonMatch.Groups[1].Value
            bitness = [int] $pythonMatch.Groups[2].Value
            pip = $pipProbe.stdout
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
        -Evidence "$($pythonSelection.display) reports Python $($pythonSelection.version), $($pythonSelection.bitness)-bit, $($pythonSelection.pip)" `
        -Remediation ""
    $checks.python["target"] = $pythonSelection.display
}

$wslExecutable = Resolve-Executable -Name "wsl.exe"
$wslReady = $false
$wslDistribution = $null
if ($null -eq $wslExecutable) {
    $checks.wsl2 = New-Check -Status "missing" `
        -Evidence "wsl.exe was not found" `
        -Remediation "Enable WSL2 explicitly in Windows Features and install an x86_64 Linux distribution."
}
else {
    $wslList = Invoke-BoundedProbe -Executable $wslExecutable -Arguments @("--list", "--verbose")
    $wslQuiet = Invoke-BoundedProbe -Executable $wslExecutable -Arguments @("--list", "--quiet")
    $verboseText = ([string] $wslList.stdout) -replace "`0", ""
    $quietText = ([string] $wslQuiet.stdout) -replace "`0", ""
    if ($wslList.exit_code -eq 0 -and $wslQuiet.exit_code -eq 0) {
        foreach ($candidateDistribution in ($quietText -split '[\r\n]+' | ForEach-Object {
            $_.Trim()
        } | Where-Object { $_.Length -gt 0 })) {
            $distributionPattern = (
                '(?m)^\s*\*?\s*' +
                [regex]::Escape($candidateDistribution) +
                '\s+\S+\s+2\s*$'
            )
            if ($verboseText -match $distributionPattern) {
                $wslDistribution = $candidateDistribution
                break
            }
        }
    }
    $wslArchitecture = $null
    $wslPython = $null
    $wslPip = $null
    $wslGit = $null
    $wslBash = $null
    $wslCurl = $null
    if ($null -ne $wslDistribution) {
        $wslArchitecture = Invoke-BoundedProbe -Executable $wslExecutable -Arguments @(
            "--distribution", $wslDistribution, "--exec", "uname", "-m"
        )
        $wslPython = Invoke-BoundedProbe -Executable $wslExecutable -Arguments @(
            "--distribution", $wslDistribution, "--exec", "python3", "-c",
            'import struct,sys;print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}|{struct.calcsize(''P'') * 8}")'
        )
        $wslPip = Invoke-BoundedProbe -Executable $wslExecutable -Arguments @(
            "--distribution", $wslDistribution, "--exec", "python3", "-m", "pip", "--version"
        )
        $wslGit = Invoke-BoundedProbe -Executable $wslExecutable -Arguments @(
            "--distribution", $wslDistribution, "--exec", "sh", "-c",
            "command -v git >/dev/null && git --version"
        )
        $wslBash = Invoke-BoundedProbe -Executable $wslExecutable -Arguments @(
            "--distribution", $wslDistribution, "--exec", "bash", "--version"
        )
        $wslCurl = Invoke-BoundedProbe -Executable $wslExecutable -Arguments @(
            "--distribution", $wslDistribution, "--exec", "curl", "--version"
        )
    }
    $hasVersionTwo = $null -ne $wslDistribution
    $distributionArchitecture = if ($null -ne $wslArchitecture) {
        ([string] $wslArchitecture.stdout).Trim()
    } else {
        ""
    }
    $hasSupportedArchitecture = (
        $null -ne $wslArchitecture -and
        $wslArchitecture.exit_code -eq 0 -and
        $distributionArchitecture -eq "x86_64"
    )
    $guestPythonMatch = if ($null -ne $wslPython) {
        [regex]::Match(
            [string] $wslPython.stdout,
            '^(3\.(?:11|12|13)\.[0-9]+)\|(64)$'
        )
    } else {
        [regex]::Match("", "never")
    }
    $guestPythonSupported = $null -ne $wslPython -and $wslPython.exit_code -eq 0 -and $guestPythonMatch.Success
    $guestPython64Bit = $guestPythonSupported -and $guestPythonMatch.Groups[2].Value -eq "64"
    $guestPip = $null -ne $wslPip -and $wslPip.exit_code -eq 0 -and $wslPip.stdout -match '^pip\s+[0-9]'
    $guestGit = $null -ne $wslGit -and $wslGit.exit_code -eq 0 -and $wslGit.stdout -match '^git version '
    $guestBash = $null -ne $wslBash -and $wslBash.exit_code -eq 0 -and $wslBash.stdout -match '^GNU bash, version '
    $guestCurl = $null -ne $wslCurl -and $wslCurl.exit_code -eq 0 -and $wslCurl.stdout -match '^curl [0-9]'
    $wslReady = (
        $hasVersionTwo -and
        $hasSupportedArchitecture -and
        $guestPythonSupported -and
        $guestPython64Bit -and
        $guestPip -and
        $guestGit -and
        $guestBash -and
        $guestCurl
    )
    if ($wslReady) {
        $checks.wsl2 = New-Check -Status "ready" `
            -Evidence "distribution=$wslDistribution; distribution_architecture=$distributionArchitecture; guest_python_supported=$guestPythonSupported; guest_python_64_bit=$guestPython64Bit; guest_pip=$guestPip; guest_git=$guestGit; guest_bash=$guestBash; guest_curl=$guestCurl" `
            -Remediation ""
        $checks.wsl2["target"] = $wslDistribution
    }
    else {
        $wslEvidence = @(
            "distribution=$wslDistribution",
            "version2=$hasVersionTwo",
            "distribution_architecture=$distributionArchitecture",
            "guest_python_supported=$guestPythonSupported",
            "guest_python_64_bit=$guestPython64Bit",
            "guest_pip=$guestPip",
            "guest_git=$guestGit",
            "guest_bash=$guestBash",
            "guest_curl=$guestCurl",
            $wslList.stderr,
            $wslQuiet.stderr
        ) -join "; "
        $checks.wsl2 = New-Check -Status "incomplete" `
            -Evidence $wslEvidence `
            -Remediation "Configure one x86_64 WSL2 distribution with git, bash, curl, 64-bit Python 3.11-3.13, and pip; this script will not enable or install them."
        $checks.wsl2["target"] = $wslDistribution
    }
}

$dockerExecutable = Resolve-Executable -Name "docker.exe"
$dockerReady = $false
$dockerContext = $null
if ($null -eq $dockerExecutable) {
    $checks.docker_desktop = New-Check -Status "missing" `
        -Evidence "docker.exe was not found on PATH" `
        -Remediation "Install and start Docker Desktop explicitly, then enable its Linux container engine."
}
else {
    $dockerContextProbe = Invoke-BoundedProbe -Executable $dockerExecutable -Arguments @(
        "context", "show"
    )
    if ($dockerContextProbe.exit_code -eq 0) {
        $dockerContext = ([string] $dockerContextProbe.stdout).Trim()
    }
    # When the docker daemon isn't reachable, `context show` fails and
    # $dockerContext stays $null; ([string] $null) is "" which fails to bind to
    # the [string[]] Arguments parameter ("Cannot bind argument ... empty
    # string"). Fall back to the standard "default" context so the downstream
    # probes bind cleanly and simply report non-zero exit codes.
    if ([string]::IsNullOrEmpty($dockerContext)) {
        $dockerContext = "default"
    }
    $dockerEndpointProbe = Invoke-BoundedProbe -Executable $dockerExecutable -Arguments @(
        "context", "inspect", ([string] $dockerContext), "--format",
        "{{.Endpoints.docker.Host}}"
    )
    $dockerEndpoint = if ($dockerEndpointProbe.exit_code -eq 0) {
        ([string] $dockerEndpointProbe.stdout).Trim()
    } else {
        ""
    }
    $dockerProbe = Invoke-BoundedProbe -Executable $dockerExecutable -Arguments @(
        "--context", ([string] $dockerContext), "info", "--format",
        "{{.OSType}}|{{.Architecture}}|{{.DockerRootDir}}|{{.Name}}"
    )
    $dockerParts = @(([string] $dockerProbe.stdout) -split '\|', 4)
    $dockerOsType = if ($dockerParts.Count -ge 1) { $dockerParts[0].Trim() } else { "" }
    $dockerArchitecture = if ($dockerParts.Count -ge 2) { $dockerParts[1].Trim() } else { "" }
    $dockerRootDir = if ($dockerParts.Count -ge 3) { $dockerParts[2].Trim() } else { "" }
    $dockerServerName = if ($dockerParts.Count -ge 4) { $dockerParts[3].Trim() } else { "" }
    $localDesktopContext = $dockerContext -eq "desktop-linux"
    $localDesktopEndpoint = (
        $dockerEndpoint -match '(?i)^npipe://' -and
        $dockerEndpoint -match '(?i)dockerDesktopLinuxEngine'
    )
    $linuxEngine = $dockerOsType -eq "linux"
    $supportedDockerArchitecture = $dockerArchitecture -in @(
        "x86_64", "amd64", "aarch64", "arm64"
    )
    $localLinuxRoot = $dockerRootDir.StartsWith("/")
    $desktopServer = $dockerServerName -match '(?i)docker-desktop'
    $dockerReady = (
        $dockerContextProbe.exit_code -eq 0 -and
        $dockerProbe.exit_code -eq 0 -and
        $localDesktopContext -and
        $localDesktopEndpoint -and
        $linuxEngine -and
        $supportedDockerArchitecture -and
        $localLinuxRoot -and
        $desktopServer
    )
    if ($dockerReady) {
        $checks.docker_desktop = New-Check -Status "ready" `
            -Evidence "docker context show=$dockerContext; local_desktop_context=$localDesktopContext; local_endpoint=$localDesktopEndpoint; linux_engine=$linuxEngine; architecture=$dockerArchitecture; root=$dockerRootDir; server=$dockerServerName" `
            -Remediation ""
        $checks.docker_desktop["target"] = $dockerContext
    }
    else {
        $checks.docker_desktop = New-Check -Status "unavailable" `
            -Evidence "docker context show=$dockerContext; local_desktop_context=$localDesktopContext; local_endpoint=$localDesktopEndpoint; linux_engine=$linuxEngine; architecture=$dockerArchitecture; root=$dockerRootDir; server=$dockerServerName; $($dockerContextProbe.stderr); $($dockerEndpointProbe.stderr); $($dockerProbe.stderr)" `
            -Remediation "Select the local Docker Desktop desktop-linux context and verify its Linux engine and supported architecture before retrying."
        $checks.docker_desktop["target"] = $dockerContext
    }
}

$nativeReady = $runningOnWindows -and $gitReady -and $null -ne $pythonSelection
$wslPathReady = $runningOnWindows -and $wslReady
$dockerPathReady = $runningOnWindows -and $dockerReady
$paths = [ordered] @{
    native_wheel = [ordered] @{
        ready = $nativeReady
        requires = @("Git for Windows", "64-bit Python 3.11-3.13", "pip")
        trust_boundary = "Version-pinned package-index resolution; not hash-bound to a verified local wheel."
        install_assurance = "version_pinned_package_index"
    }
    wsl2 = [ordered] @{
        ready = $wslPathReady
        target = $wslDistribution
        requires = @(
            "WSL2",
            "x86_64 Linux distribution",
            "git",
            "64-bit Python 3.11-3.13",
            "pip",
            "bash",
            "curl"
        )
        trust_boundary = "The existing Bash installer runs inside WSL2, not Git Bash."
        install_assurance = "release_script_transport"
    }
    docker_desktop = [ordered] @{
        ready = $dockerPathReady
        target = $dockerContext
        requires = @("local Docker Desktop desktop-linux context", "Linux engine")
        trust_boundary = "Published Kestrel container; host prerequisites are not modified."
        install_assurance = "version_pinned_container_tag"
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
            "wsl.exe --distribution `"$wslDistribution`" -- bash -c `"curl -fsSL https://github.com/John-MiracleWorker/Kestrel/releases/download/v$Version/install.sh | bash`"",
            "wsl.exe --distribution `"$wslDistribution`" -- kestrel doctor"
        )
    }
    elseif ($selectedPath -eq "docker_desktop") {
        $commands = @(
            "docker --context desktop-linux pull ghcr.io/john-miracleworker/kestrel:v$Version",
            "docker --context desktop-linux run --rm ghcr.io/john-miracleworker/kestrel:v$Version nest-agent doctor --backend memory --provider mock"
        )
    }
}
$installAssurance = $null
$assuranceNote = ""
if ($null -ne $selectedPath) {
    $installAssurance = $paths[$selectedPath].install_assurance
    if ($selectedPath -eq "native_wheel") {
        $assuranceNote = (
            "This is a version-pinned package-index install, not hash-bound to a " +
            "verified local wheel. Review index and provenance before execution."
        )
    }
}

$report = [ordered] @{
    schema = "kestrel.windows_bootstrap_report.v1"
    platform = [ordered] @{
        native_windows = $runningOnWindows
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
        install_assurance = $installAssurance
        assurance_note = $assuranceNote
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
        if ($assuranceNote.Length -gt 0) {
            Write-Output "  Assurance: $assuranceNote"
        }
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
