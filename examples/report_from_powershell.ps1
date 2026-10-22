# Report a job to the Factory Console from PowerShell (scheduled tasks, backup scripts, build scripts...).
function Send-Feed([string]$Name, [string]$State = 'running', $Progress = $null, [string]$Log = '', [string]$FramesDir = '') {
    $body = @{ name = $Name; state = $State; progress = $Progress; log = $Log; frames_dir = $FramesDir } | ConvertTo-Json -Compress
    try { Invoke-RestMethod -Uri 'http://127.0.0.1:8787/api/feed' -Method Post -Body $body -ContentType 'application/json' -TimeoutSec 2 | Out-Null } catch { }
}

# example: a backup that reports as it goes
$folders = 'Documents', 'Pictures', 'Videos'
for ($i = 0; $i -lt $folders.Count; $i++) {
    Send-Feed 'nightly backup' 'running' (($i) / $folders.Count) "copying $($folders[$i])"
    Start-Sleep -Seconds 2          # ... your real work here ...
}
Send-Feed 'nightly backup' 'done' 1 "$($folders.Count) folders copied"
