<#
.SYNOPSIS
Read-only preflight for the Coroner demo (Windows PowerShell 5.1 and PowerShell 7).

.DESCRIPTION
Checks, each reported as PASS or FAIL:
  1. .venv\Scripts\python.exe is Python 3.12.x
  2. import coroner.viewer succeeds
  3. each canonical run directory holds manifest.json, evidence.jsonl, docker_inspect.json
  4. port 8765 is free, otherwise 8766 is free (an occupying process is reported, never stopped)
  5. with -LaunchViewer only: start the Viewer and stop only that process at the end
  6. HTTP 200 for /, /api/runs, /runs/<id> x3, /api/runs/<id> x3 and /compare (Host: 127.0.0.1:<port>)
  7. ExitCode and OOMKilled from /api/runs/<id> match the demo facts (A 1/false, B 137/false, C 137/true)
  8. SHA-256 and size of every file in the three run directories match the baseline file

The script never writes under runs/, never writes the baseline, never runs Docker,
and never stops a process it did not start. Exit code 0 only if every check passed.

-BaselinePath must resolve outside the runs directory and outside the repository. The
baseline file is read only. Line format (the same as the M1 snapshot):
  <run-id>/<file> <SHA256 hex> <size in bytes>

-Offline skips every HTTP check and the Viewer launch.
-Json prints one JSON object ({ ok, checks[] }) instead of plain text.
#>
[CmdletBinding()]
param(
    [string]$RunsDir = 'runs',
    [string[]]$RunIds = @(
        'a-20261120T211146Z-4300f395',
        'b-20261120T211311Z-ce313da1',
        'c-20261120T211433Z-09b81ca1'
    ),
    [int]$Port = 8765,
    [int]$FallbackPort = 8766,
    [string]$BaselinePath,
    [switch]$Json,
    [switch]$Offline,
    [switch]$LaunchViewer
)

$ErrorActionPreference = 'Continue'
Set-StrictMode -Version 2.0

# With "powershell -File", -RunIds a,b,c arrives as one string "a,b,c"; split it.
$RunIds = @($RunIds | ForEach-Object { $_ -split ',' } | Where-Object { $_.Trim() } | ForEach-Object { $_.Trim() })

$RepoRoot = Split-Path -Parent $PSScriptRoot
$script:Checks = New-Object System.Collections.ArrayList
$script:LaunchedPid = $null

# Demo facts (ExitCode, OOMKilled) keyed by canonical run id.
$Expected = @{
    'a-20261120T211146Z-4300f395' = @{ Label = 'A'; ExitCode = 1;   OomKilled = $false }
    'b-20261120T211311Z-ce313da1' = @{ Label = 'B'; ExitCode = 137; OomKilled = $false }
    'c-20261120T211433Z-09b81ca1' = @{ Label = 'C'; ExitCode = 137; OomKilled = $true }
}

function Add-Check {
    param([string]$Name, [bool]$Ok, [string]$Message)
    $status = 'FAIL'
    if ($Ok) { $status = 'PASS' }
    [void]$script:Checks.Add([pscustomobject]@{ name = $Name; status = $status; message = $Message })
}

function Resolve-Full {
    param([string]$Path, [string]$Base)
    if ([System.IO.Path]::IsPathRooted($Path)) { return [System.IO.Path]::GetFullPath($Path) }
    return [System.IO.Path]::GetFullPath((Join-Path $Base $Path))
}

function Test-Inside {
    param([string]$Child, [string]$Parent)
    $p = $Parent.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    return $Child.StartsWith($p, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-PortOwner {
    # Returns $null when the port is free, else an object with Pid and Name (read-only query).
    param([int]$P)
    $conn = $null
    try {
        $conn = Get-NetTCPConnection -State Listen -LocalPort $P -ErrorAction Stop | Select-Object -First 1
    } catch { $conn = $null }
    if ($conn) {
        $procName = 'unknown'
        $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        if ($proc) { $procName = $proc.ProcessName }
        return [pscustomobject]@{ Pid = [int]$conn.OwningProcess; Name = $procName }
    }
    # Fallback: a plain connect attempt on loopback.
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect('127.0.0.1', $P, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne(500) -and $client.Connected) {
            return [pscustomobject]@{ Pid = 0; Name = 'unknown' }
        }
    } catch { } finally { $client.Close() }
    return $null
}

function Describe-Owner {
    param($Owner)
    if ($Owner.Pid -gt 0) { return ('process {0} (PID {1})' -f $Owner.Name, $Owner.Pid) }
    return 'a process that could not be identified'
}

function Invoke-Get {
    # GET with an explicit Host header. Returns Status (0 on connection failure) and Body.
    param([int]$P, [string]$PathAndQuery)
    $result = [pscustomobject]@{ Status = 0; Body = ''; Error = '' }
    try {
        $req = [System.Net.HttpWebRequest]::Create('http://127.0.0.1:' + $P + $PathAndQuery)
        $req.Method = 'GET'
        $req.Host = '127.0.0.1:' + $P
        $req.Timeout = 10000
        $req.ReadWriteTimeout = 10000
        $req.AllowAutoRedirect = $false
        $resp = $null
        try {
            $resp = $req.GetResponse()
        } catch [System.Net.WebException] {
            if ($_.Exception.Response) { $resp = $_.Exception.Response } else { throw }
        }
        try {
            $result.Status = [int]$resp.StatusCode
            $reader = New-Object System.IO.StreamReader($resp.GetResponseStream(), [System.Text.Encoding]::UTF8)
            $result.Body = $reader.ReadToEnd()
            $reader.Close()
        } finally { $resp.Close() }
    } catch {
        $result.Error = $_.Exception.Message
    }
    return $result
}

function Get-Descendants {
    param([int]$RootPid)
    $all = @()
    $queue = New-Object System.Collections.Queue
    $queue.Enqueue($RootPid)
    while ($queue.Count -gt 0) {
        $cur = [int]$queue.Dequeue()
        $kids = Get-CimInstance -ClassName Win32_Process -Filter ('ParentProcessId = ' + $cur) -ErrorAction SilentlyContinue
        foreach ($k in @($kids)) {
            if ($k) { $all += [int]$k.ProcessId; $queue.Enqueue([int]$k.ProcessId) }
        }
    }
    return $all
}

function Stop-OwnViewer {
    # Stops only the Viewer process (and its children) that this script started.
    if ($null -eq $script:LaunchedPid) { return }
    $ids = @(Get-Descendants -RootPid $script:LaunchedPid)
    [array]::Reverse($ids)
    foreach ($id in $ids) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
    Stop-Process -Id $script:LaunchedPid -Force -ErrorAction SilentlyContinue
    $script:LaunchedPid = $null
}

function Write-Report {
    $ok = $true
    foreach ($c in $script:Checks) { if ($c.status -ne 'PASS') { $ok = $false } }
    if ($Json) {
        $obj = [ordered]@{ ok = $ok; checks = @($script:Checks | ForEach-Object { $_ }) }
        Write-Output (ConvertTo-Json -InputObject $obj -Depth 5)
    } else {
        foreach ($c in $script:Checks) {
            Write-Output ('[{0}] {1}: {2}' -f $c.status, $c.name, $c.message)
        }
        $failed = @($script:Checks | Where-Object { $_.status -ne 'PASS' }).Count
        if ($ok) { Write-Output ('RESULT: PASS ({0} checks)' -f $script:Checks.Count) }
        else { Write-Output ('RESULT: FAIL ({0} of {1} checks failed)' -f $failed, $script:Checks.Count) }
    }
    $script:ReportOk = $ok
}

try {
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $runsFull = Resolve-Full -Path $RunsDir -Base $RepoRoot
    $py = Join-Path $RepoRoot '.venv\Scripts\python.exe'

    # 1. Python version
    if (-not (Test-Path -LiteralPath $py)) {
        Add-Check 'python-version' $false ('missing ' + $py)
        Add-Check 'import-viewer' $false 'skipped: virtual environment python not found'
    } else {
        $ver = (& $py --version 2>&1 | Out-String).Trim()
        Add-Check 'python-version' ($ver -match '^Python 3\.12\.\d+$') ('.venv python reports: ' + $ver)

        # 2. import coroner.viewer
        Push-Location $RepoRoot
        try {
            $imp = (& $py -c 'import coroner.viewer' 2>&1 | Out-String).Trim()
            $impOk = ($LASTEXITCODE -eq 0)
        } finally { Pop-Location }
        if ($impOk) { Add-Check 'import-viewer' $true 'import coroner.viewer succeeded' }
        else { Add-Check 'import-viewer' $false ('import coroner.viewer failed: ' + $imp) }
    }

    # 3. canonical run directories and files
    $required = @('manifest.json', 'evidence.jsonl', 'docker_inspect.json')
    foreach ($id in $RunIds) {
        $dir = Join-Path $runsFull $id
        if (-not (Test-Path -LiteralPath $dir -PathType Container)) {
            Add-Check ('run-files ' + $id) $false 'run directory not found'
            continue
        }
        $missing = @($required | Where-Object { -not (Test-Path -LiteralPath (Join-Path $dir $_) -PathType Leaf) })
        if ($missing.Count -eq 0) { Add-Check ('run-files ' + $id) $true 'manifest.json, evidence.jsonl, docker_inspect.json present' }
        else { Add-Check ('run-files ' + $id) $false ('missing: ' + ($missing -join ', ')) }
    }

    # 4. ports
    $own1 = Get-PortOwner -P $Port
    $own2 = Get-PortOwner -P $FallbackPort
    $msg = @()
    if ($own1) { $msg += ('port {0} is occupied by {1}' -f $Port, (Describe-Owner $own1)) } else { $msg += ('port {0} is free' -f $Port) }
    if ($own2) { $msg += ('port {0} is occupied by {1}' -f $FallbackPort, (Describe-Owner $own2)) } else { $msg += ('port {0} is free' -f $FallbackPort) }
    $portOk = ((-not $own1) -or (-not $own2))
    $note = ' (no process was stopped)'
    if (-not $portOk) { $note = ': neither port is free (no process was stopped)' }
    Add-Check 'ports' $portOk (($msg -join '; ') + $note)

    # Choose HTTP target
    $httpPort = 0
    if (-not $Offline) {
        if ($LaunchViewer) {
            # 5. launch on the first free port
            $launchPort = 0
            if (-not $own1) { $launchPort = $Port } elseif (-not $own2) { $launchPort = $FallbackPort }
            if ($launchPort -eq 0) {
                Add-Check 'launch-viewer' $false 'no free port to launch the Viewer on'
            } elseif (-not (Test-Path -LiteralPath $py)) {
                Add-Check 'launch-viewer' $false 'virtual environment python not found'
            } else {
                $argList = @('-m', 'coroner.viewer', '--runs-dir', $runsFull, '--host', '127.0.0.1', '--port', [string]$launchPort)
                $proc = Start-Process -FilePath $py -ArgumentList $argList -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru
                $script:LaunchedPid = $proc.Id
                $ready = $false
                for ($i = 0; $i -lt 30; $i++) {
                    Start-Sleep -Milliseconds 500
                    if ($proc.HasExited) { break }
                    $probe = Invoke-Get -P $launchPort -PathAndQuery '/api/runs'
                    if ($probe.Status -eq 200) { $ready = $true; break }
                }
                if ($ready) {
                    $httpPort = $launchPort
                    Add-Check 'launch-viewer' $true ('started Viewer PID {0} on 127.0.0.1:{1}; it is stopped at the end' -f $proc.Id, $launchPort)
                } else {
                    Add-Check 'launch-viewer' $false ('Viewer did not become ready on port {0}' -f $launchPort)
                }
            }
        } else {
            if ($own1) { $httpPort = $Port } elseif ($own2) { $httpPort = $FallbackPort }
            if ($httpPort -eq 0) {
                Add-Check 'viewer-running' $false ('no Viewer is listening on {0} or {1}; start one or use -LaunchViewer' -f $Port, $FallbackPort)
            }
        }
    }

    # 6 and 7. HTTP checks and demo facts
    if ($Offline) {
        Add-Check 'http' $true 'skipped (-Offline)'
    } elseif ($httpPort -gt 0) {
        $paths = @('/', '/api/runs')
        foreach ($id in $RunIds) { $paths += ('/runs/' + $id) }
        foreach ($id in $RunIds) { $paths += ('/api/runs/' + $id) }
        if ($RunIds.Count -ge 3) {
            $paths += ('/compare?a={0}&b={1}&c={2}' -f $RunIds[0], $RunIds[1], $RunIds[2])
        }
        $bodies = @{}
        foreach ($p in $paths) {
            $r = Invoke-Get -P $httpPort -PathAndQuery $p
            $bodies[$p] = $r.Body
            if ($r.Status -eq 200) {
                Add-Check ('http ' + $p) $true ('200 from 127.0.0.1:' + $httpPort)
            } elseif ($r.Status -eq 0) {
                Add-Check ('http ' + $p) $false ('no response: ' + $r.Error)
            } else {
                Add-Check ('http ' + $p) $false ('status ' + $r.Status + ', expected 200')
            }
        }
        foreach ($id in $RunIds) {
            $name = 'facts ' + $id
            if (-not $Expected.ContainsKey($id)) { Add-Check $name $false 'not a canonical run id; no expected facts'; continue }
            $exp = $Expected[$id]
            $body = $bodies['/api/runs/' + $id]
            if (-not $body) { Add-Check $name $false 'no detail body to read'; continue }
            try {
                $d = ConvertFrom-Json -InputObject $body
                $eff = $d.docker_state.effective
                $gotExit = $eff.exit_code
                $gotOom = $eff.oom_killed
                $ok = (($gotExit -eq $exp.ExitCode) -and ($gotOom -is [bool]) -and ($gotOom -eq $exp.OomKilled))
                $m = '{0}: ExitCode {1}, OOMKilled {2} (expected {3}, {4})' -f $exp.Label, $gotExit, $gotOom, $exp.ExitCode, $exp.OomKilled
                Add-Check $name $ok $m
            } catch {
                Add-Check $name $false ('could not read ExitCode/OOMKilled from the detail response: ' + $_.Exception.Message)
            }
        }
    }

    # 8. evidence integrity against the baseline (read-only)
    if ([string]::IsNullOrEmpty($BaselinePath)) {
        Add-Check 'integrity' $false 'BASELINE MISSING: -BaselinePath was not given; integrity not checked'
    } else {
        $baseFull = Resolve-Full -Path $BaselinePath -Base (Get-Location).Path
        if ((Test-Inside $baseFull $runsFull) -or (Test-Inside $baseFull $RepoRoot)) {
            Add-Check 'integrity' $false 'baseline path must be outside runs/ and outside the repository'
        } elseif (-not (Test-Path -LiteralPath $baseFull -PathType Leaf)) {
            Add-Check 'integrity' $false 'BASELINE MISSING: file not found (task 2.2 creates it); integrity not checked'
        } else {
            $entries = @{}
            foreach ($line in [System.IO.File]::ReadAllLines($baseFull)) {
                if ($line -match '^(\S.*?) ([0-9A-Fa-f]{64}) (\d+)\s*$') {
                    $entries[$Matches[1].Replace('\', '/')] = @{ Hash = $Matches[2].ToUpperInvariant(); Size = [int64]$Matches[3] }
                }
            }
            $diffs = @()
            $count = 0
            foreach ($id in $RunIds) {
                $dir = Join-Path $runsFull $id
                $onDisk = @{}
                if (Test-Path -LiteralPath $dir -PathType Container) {
                    foreach ($f in (Get-ChildItem -LiteralPath $dir -Recurse -File)) {
                        $rel = $id + '/' + $f.FullName.Substring($dir.TrimEnd('\').Length + 1).Replace('\', '/')
                        $onDisk[$rel] = $f
                    }
                }
                $inBase = @($entries.Keys | Where-Object { $_.StartsWith($id + '/') })
                if ($inBase.Count -eq 0) { $diffs += ('no baseline entries for ' + $id) }
                foreach ($k in $inBase) {
                    if (-not $onDisk.ContainsKey($k)) { $diffs += ('missing on disk: ' + $k); continue }
                    $count++
                    $f = $onDisk[$k]
                    if ($f.Length -ne $entries[$k].Size) { $diffs += ('size differs: ' + $k); continue }
                    $h = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash.ToUpperInvariant()
                    if ($h -ne $entries[$k].Hash) { $diffs += ('hash differs: ' + $k) }
                }
                foreach ($k in $onDisk.Keys) {
                    if (-not $entries.ContainsKey($k)) { $diffs += ('not in baseline: ' + $k) }
                }
            }
            if ($diffs.Count -eq 0) { Add-Check 'integrity' $true ('{0} files match the baseline' -f $count) }
            else { Add-Check 'integrity' $false ('{0} difference(s): {1}' -f $diffs.Count, ($diffs -join '; ')) }
        }
    }
} catch {
    Add-Check 'script-error' $false $_.Exception.Message
} finally {
    Stop-OwnViewer
}

$script:ReportOk = $false
Write-Report
if ($script:ReportOk) { exit 0 } else { exit 1 }
