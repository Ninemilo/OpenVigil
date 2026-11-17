. "$PSScriptRoot/Local-Common.ps1"
[IO.Directory]::CreateDirectory($script:LocalFolder) | Out-Null
$sentinelFile = Join-Path $script:LocalFolder "ownership-sentinel-$([guid]::NewGuid().ToString('N')).ps1"
Set-Content -LiteralPath $sentinelFile -Value 'Start-Sleep -Seconds 120'
$record = $null
$startupSentinel = $null
$script:SimulateIncompleteStartup = $false
function Get-CimInstance {
    param([string]$ClassName, [string]$Filter)
    $observed = CimCmdlets\Get-CimInstance -ClassName $ClassName -Filter $Filter
    if ($script:SimulateIncompleteStartup -and $null -ne $observed -and
        $observed.ParentProcessId -eq $PID -and $observed.CommandLine -and
        $observed.CommandLine.Contains($sentinelFile, [StringComparison]::OrdinalIgnoreCase)) {
        $script:SimulateIncompleteStartup = $false
        $script:startupSentinel = Get-Process -Id $observed.ProcessId
        $null = $script:startupSentinel.Handle
        # The process has started, but CIM has not yet published all identity fields.
        return [PSCustomObject]@{
            ProcessId = $observed.ProcessId
            CreationDate = $observed.CreationDate
            CommandLine = $observed.CommandLine
            ExecutablePath = $null
        }
    }
    return $observed
}
try {
    $mixedPath = $sentinelFile.Replace('\', '/').ToLowerInvariant()
    if (-not (Test-LocalCheckoutCommand ('pwsh -File "' + $mixedPath + '"'))) {
        throw 'Windows path case or separators rejected an owned command.'
    }
    if (Test-LocalCheckoutCommand ('pwsh -File "' + $script:LocalRoot + '-other/sentinel.ps1"')) {
        throw 'Sibling checkout was accepted as owned.'
    }
    $shell = (Get-Process -Id $PID).Path
    $script:SimulateIncompleteStartup = $true
    $record = Start-OwnedProcess 'ownership-test' $shell @('-NoProfile', '-File', $sentinelFile)
    if ($script:SimulateIncompleteStartup) { throw 'Incomplete startup identity was not exercised.' }
    if ($null -eq (Get-OwnedProcess $record)) { throw 'Sentinel was not started.' }
    $roundTrip = $record | ConvertTo-Json | ConvertFrom-Json
    if ($null -eq (Get-OwnedProcess $roundTrip)) { throw 'Serialized identity cannot be revalidated.' }
    $originalCreation = $record.created
    $record.created = 'wrong-creation-time'
    $rejected = $false
    try { Stop-OwnedProcess $record } catch { $rejected = $true }
    if (-not $rejected) { throw 'Mismatched PID identity was accepted.' }
    $record.created = $originalCreation
    if ($null -eq (Get-OwnedProcess $record)) { throw 'Identity rejection killed the sentinel.' }
    Stop-OwnedProcess $record
    if ($null -ne (Get-OwnedProcess $record)) { throw 'Owned sentinel was not stopped.' }
    Stop-OwnedProcess $record
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        $port = $listener.LocalEndpoint.Port
        $rejected = $false
        try { Assert-LocalPortFree $port } catch { $rejected = $true }
        if (-not $rejected) { throw 'Occupied port was accepted.' }
        if (-not $listener.Server.IsBound) { throw 'Preflight affected another port owner.' }
    } finally { $listener.Stop() }
    Write-Output 'PASS: Incomplete startup identity retried, path variants accepted, sibling checkout and recycled PID rejected, owned stop idempotent, occupied port preserved.'
} finally {
    try {
        if ($null -ne $record) { Stop-OwnedProcess $record }
    } finally {
        if ($null -ne $startupSentinel) {
            if (-not $startupSentinel.HasExited) {
                $startupSentinel.Kill()
                if (-not $startupSentinel.WaitForExit(5000)) { throw 'Sentinel did not stop.' }
            }
            $startupSentinel.Dispose()
        }
        Remove-Item -LiteralPath $sentinelFile
    }
}
