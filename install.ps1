# install.ps1 - Factory Console: a private Python environment with pywebview, a config.json, and a Startup shortcut for the
# desktop drop-down. No admin rights needed. Safe to run again (it only adds what is missing).
#   powershell -ExecutionPolicy Bypass -File install.ps1            install + start
#   ... -NoStartup                                                   do not start it with Windows
#   ... -NoLaunch                                                    do not start it now
param([switch]$NoStartup, [switch]$NoLaunch)
$ErrorActionPreference = 'Continue'     # not 'Stop': Windows PowerShell 5.1 turns any stderr line of a probe (py -3.x, import webview) into a fatal error
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here
function Say($m) { Write-Host "  $m" }
Write-Host "Factory Console setup in $here"

# 1. Python 3.10-3.13 (pywebview talks to WebView2 through pythonnet, which can lag behind the newest Python)
$venvPy = Join-Path $here '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    $py = $null
    foreach ($v in '3.13', '3.12', '3.11', '3.10') {
        $p = & py "-$v" -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $p) { $py = "$p".Trim(); break }
    }
    if (-not $py) { throw "Python 3.10-3.13 was not found. Install it from https://www.python.org (tick 'py launcher'), then run this again." }
    Say "creating .venv with $py"
    & $py -m venv .venv
}

# 2. WebView2 runtime (ships with Windows 11 and current Edge)
$wv = Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}' -ErrorAction SilentlyContinue
if (-not $wv) { $wv = Get-ItemProperty 'HKCU:\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}' -ErrorAction SilentlyContinue }
if ($wv) { Say "WebView2 runtime $($wv.pv) found" } else { Say "WebView2 runtime not found: get it from https://developer.microsoft.com/microsoft-edge/webview2/ (the browser page still works without it)" }

# 3. pywebview (only when it is missing)
& $venvPy -c "import webview" 2>$null
if ($LASTEXITCODE -ne 0) {
    & $venvPy -m pip --version 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { & $venvPy -m ensurepip --upgrade | Out-Null }
    Say "installing pywebview"
    & $venvPy -m pip install --disable-pip-version-check -q "pywebview>=5"
} else { Say "pywebview already installed" }

# 4. config.json: the first import of server.py writes the defaults (edit it afterwards)
if (-not (Test-Path (Join-Path $here 'config.json'))) { & $venvPy -c "import server" ; Say "wrote config.json with the defaults: open it to choose what Game Mode pauses" }
else { Say "config.json kept" }

# 5. start with Windows: a shortcut in the user's Startup folder
$pyw = Join-Path $here '.venv\Scripts\pythonw.exe'
if (-not $NoStartup) {
    $lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'Factory Console.lnk'
    $ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut($lnk)
    $s.TargetPath = $pyw; $s.Arguments = "`"$here\shell.py`""; $s.WorkingDirectory = $here
    $s.Description = 'Factory Console: push the mouse to the top edge, or press Ctrl+Alt+F'; $s.Save()
    Say "Startup shortcut: $lnk"
}

# 6. start it now
if (-not $NoLaunch) {
    Start-Process $pyw -ArgumentList "`"$here\shell.py`"" -WorkingDirectory $here
    Say "started: push the mouse against the top edge of the screen, on the small bar in the middle (or press Ctrl+Alt+F)"
    Say "browser view: http://127.0.0.1:8787"
}
Write-Host "Done."
