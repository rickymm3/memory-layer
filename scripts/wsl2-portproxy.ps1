#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Update Windows port-proxy rules so the Mac (and any LAN device) can reach
  the WSL2 Postgres database and the memory-layer MCP HTTP server.

.DESCRIPTION
  WSL2 gets a fresh internal IP on every restart, which breaks static portproxy
  rules. This script re-reads the current WSL2 IP and replaces the rules.

  Run once after each WSL2 restart, or schedule it as a Windows startup task:
    schtasks /Create /SC ONLOGON /RL HIGHEST /TN "WSL2 memory-layer portproxy" `
             /TR "powershell -NonInteractive -File '<full path to this script>'"

  Ports forwarded:
    5432  PostgreSQL (pgvector)
    8765  memory-layer MCP HTTP server  (make mcp-serve)
#>

$Ports = @(5432, 8765)

# Resolve current WSL2 IP
$WslIp = (wsl hostname -I 2>$null).Trim().Split(" ")[0]
if (-not $WslIp) {
    Write-Error "Cannot determine WSL2 IP — is WSL running? Start it first with: wsl"
    exit 1
}
Write-Host "WSL2 IP: $WslIp"

foreach ($Port in $Ports) {
    # Remove stale rule (ignore errors if it didn't exist)
    netsh interface portproxy delete v4tov4 listenport=$Port listenaddress=0.0.0.0 2>$null | Out-Null
    # Add fresh rule pointing at current WSL2 IP
    netsh interface portproxy add v4tov4 `
        listenport=$Port listenaddress=0.0.0.0 `
        connectport=$Port connectaddress=$WslIp
    Write-Host "  0.0.0.0:$Port  ->  ${WslIp}:$Port"
}

# Ensure inbound firewall rules exist (idempotent)
foreach ($Port in $Ports) {
    $RuleName = "WSL2 memory-layer $Port"
    if (-not (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $RuleName -Direction Inbound `
            -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
        Write-Host "  Firewall rule created: $RuleName"
    } else {
        Write-Host "  Firewall rule OK: $RuleName"
    }
}

Write-Host ""
Write-Host "Active portproxy rules:"
netsh interface portproxy show v4tov4
