# uninstall.ps1 - stops the Factory Console drop-down and removes its Startup shortcut.
# It deletes nothing else: delete this folder yourself if you want it gone (config.json, state.json and .venv live here).
# If Game Mode is ON when you run this, turn it off first (in the panel), so no work process stays frozen.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$st = Join-Path $here 'state.json'
if ((Test-Path $st) -and ((Get-Content $st -Raw | ConvertFrom-Json).game_mode)) { Write-Host "Game Mode is ON: turn it off in the panel first, then run this again."; exit 1 }
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -and ($_.CommandLine.Contains("$here\shell.py") -or $_.CommandLine.Contains("$here\server.py")) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host "  stopped pid $($_.ProcessId)" }
$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'Factory Console.lnk'
if (Test-Path $lnk) { Remove-Item $lnk; Write-Host "  removed $lnk" }
Write-Host "Done. The folder $here is still here."
