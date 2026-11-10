<#
.SYNOPSIS
  Install or update the TV Remote hub for the signed-in Windows user and keep it running.

.DESCRIPTION
  Creates a private Python environment in %LOCALAPPDATA%\tvremote\venv, installs this
  repository into it, starts the hub without a window, and registers it to start at
  every sign-in (per-user Run entry, no administrator rights needed).

  The hub listens on 127.0.0.1:8080 only. Its settings, pairing and access key live in
  %USERPROFILE%\.config\tvremote, its log in hub.log there.

  Run it from a clone that only you can write to, because the code will run as you:
      powershell -ExecutionPolicy Bypass -File deploy\windows\install-hub.ps1
  Run it again after `git pull` to update. Remove the hub (settings are kept):
      powershell -ExecutionPolicy Bypass -File deploy\windows\install-hub.ps1 -Uninstall
#>
param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$Name    = "TV Remote Hub"
$Root    = Join-Path $env:LOCALAPPDATA "tvremote"
$Venv    = Join-Path $Root "venv"
$Pythonw = Join-Path $Venv "Scripts\pythonw.exe"
$Python  = Join-Path $Venv "Scripts\python.exe"
$Repo    = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$RunKey  = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$Health  = "http://127.0.0.1:8080/api/health"

function Stop-Hub {
    Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like "*tvremote.hub*" -and $_.ExecutablePath -like "$Venv*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Test-Hub {
    try {
        $r = Invoke-RestMethod -Uri $Health -TimeoutSec 3 -Proxy $null
        return ($r.ok -eq $true -and $r.version)
    } catch { return $false }
}

if ($Uninstall) {
    Stop-Hub
    Remove-ItemProperty -Path $RunKey -Name $Name -ErrorAction SilentlyContinue
    if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }
    Write-Host "Removed the hub. Pairing and settings are still in $env:USERPROFILE\.config\tvremote."
    exit 0
}

# Python 3.11 or newer, whichever launcher this PC has.
$Base = $null
foreach ($try in @(@("py", "-3"), @("python"), @("python3"))) {
    try {
        $exe = $try[0]
        $rest = @($try | Select-Object -Skip 1)
        $v = & $exe @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and [version]$v -ge [version]"3.11") { $Base = $try; break }
    } catch { }
}
if (-not $Base) { throw "Python 3.11 or newer is needed. Install it from python.org (tick 'Add python.exe to PATH') and run this again." }

New-Item -ItemType Directory -Force -Path $Root | Out-Null
if (-not (Test-Path $Python)) {
    $exe = $Base[0]
    $rest = @($Base | Select-Object -Skip 1)
    & $exe @rest -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Python environment in $Venv." }
}

Stop-Hub   # files in use cannot be replaced
& $Python -m pip install --quiet --disable-pip-version-check --upgrade $Repo
if ($LASTEXITCODE -ne 0) { throw "Installing the hub from $Repo failed (see the pip output above)." }

Set-ItemProperty -Path $RunKey -Name $Name -Value "`"$Pythonw`" -m tvremote.hub"
Start-Process -FilePath $Pythonw -ArgumentList "-m", "tvremote.hub" -WindowStyle Hidden

$up = $false
foreach ($i in 1..20) {
    Start-Sleep -Seconds 1
    if (Test-Hub) { $up = $true; break }
}
$Config = Join-Path $env:USERPROFILE ".config\tvremote"
if (-not $up) {
    Write-Host "The hub did not answer at $Health within 20 seconds."
    Write-Host "Look at $Config\hub.log. If another program already uses port 8080, the log says so."
    exit 1
}
$version = & $Python -c "import tvremote; print(tvremote.__version__)"
Write-Host "TV Remote hub $version is running at http://127.0.0.1:8080 and will start at sign-in."
if (Test-Path (Join-Path $Config "tv.json")) {
    Write-Host "Paired TV found. Check it with:  `"$Python`" -m tvremote selftest"
} else {
    Write-Host "No TV is paired yet. With the TV on:  `"$Python`" -m tvremote discover"
    Write-Host "then:  `"$Python`" -m tvremote pair --host <TV address>   (press Allow on the TV)"
    Write-Host "or ask Claude to pair it (the pair_tv tool) while you are at the TV."
}
