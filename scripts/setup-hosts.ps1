param(
    [string]$Domain    = "atlas.local",
    [string[]]$Apps    = @("drive", "photos", "admin"),
    [int]$FrontendPort = 5173,
    [switch]$Remove
)

# Re-launch as Administrator if not already elevated
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $extraArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Domain $Domain -FrontendPort $FrontendPort"
    if ($Remove) { $extraArgs += " -Remove" }
    Start-Process powershell -ArgumentList $extraArgs -Verb RunAs
    exit
}

$hostsFile = "$env:SystemRoot\System32\drivers\etc\hosts"

if ($Remove) {
    netsh interface portproxy delete v4tov4 listenport=80 listenaddress=0.0.0.0 | Out-Null
    $allHosts = @($Domain) + ($Apps | ForEach-Object { "$_.$Domain" })
    $lines = Get-Content $hostsFile | Where-Object {
        $line = $_.Trim()
        -not ($line -eq "# Atlas local dev") -and
        -not ($allHosts | Where-Object { $line -match [regex]::Escape($_) })
    }
    Set-Content $hostsFile ($lines -join "`n")
    Write-Host "Removed port proxy and hosts entries for $Domain."
    Read-Host "Press Enter to close"
    exit
}

# Get LAN IP (non-loopback) — portproxy cannot forward 127.0.0.1 -> 127.0.0.1
$localIP = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -ne "127.0.0.1" -and $_.PrefixOrigin -ne "WellKnown" } |
    Sort-Object -Property PrefixLength |
    Select-Object -First 1).IPAddress

if (-not $localIP) {
    Write-Host "ERROR: Could not detect a local network IP. Make sure you are connected to a network."
    Read-Host "Press Enter to close"
    exit 1
}

Write-Host "Using local IP: $localIP"

# Ensure IP Helper service is running (required for portproxy)
if ((Get-Service iphlpsvc).Status -ne "Running") {
    Start-Service iphlpsvc
    Write-Host "  Started IP Helper service"
}

# Hosts file
$entries = @("$localIP`t$Domain") + ($Apps | ForEach-Object { "$localIP`t$_.$Domain" })
$current = Get-Content $hostsFile -Raw

# Remove stale entries first (IP may have changed)
$allHosts = @($Domain) + ($Apps | ForEach-Object { "$_.$Domain" })
$cleaned = Get-Content $hostsFile | Where-Object {
    $line = $_.Trim()
    -not ($line -eq "# Atlas local dev") -and
    -not ($allHosts | Where-Object { $line -match [regex]::Escape($_) })
}
$newContent = ($cleaned -join "`n") + "`n`n# Atlas local dev`n" + ($entries -join "`n") + "`n"
[System.IO.File]::WriteAllText($hostsFile, $newContent, [System.Text.Encoding]::ASCII)
Write-Host "  hosts: set $($entries.Count) entries -> $localIP"

# Port proxy: LAN IP:80 -> 127.0.0.1:FrontendPort
netsh interface portproxy delete v4tov4 listenport=80 listenaddress=0.0.0.0 | Out-Null
netsh interface portproxy add v4tov4 listenport=80 listenaddress=0.0.0.0 connectport=$FrontendPort connectaddress=127.0.0.1 | Out-Null
Write-Host "  portproxy: $localIP`:80 -> 127.0.0.1:$FrontendPort"

Write-Host ""
Write-Host "Done. Open http://$Domain in your browser."
Write-Host "NOTE: If your IP changes, re-run this script."
Write-Host "To undo: .\scripts\setup-hosts.ps1 -Remove"
Read-Host "Press Enter to close"
