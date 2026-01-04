# apply_workspace_patch.ps1
# Usage: Run this in the repo root in PowerShell:
#   .\apply_workspace_patch.ps1 .\workspace_changes.patch.txt
param(
    [Parameter(Mandatory=$true)][string]$PatchFile
)

if (-not (Test-Path $PatchFile)) {
    Write-Error "Patch file not found: $PatchFile"
    exit 2
}

$currentFile = $null
$outPath = $PWD

Get-Content -Raw $PatchFile -Encoding UTF8 | ForEach-Object {
    $lines = $_ -split "\r?\n"
    $buffer = @()
    foreach ($line in $lines) {
        if ($line -match '^=== FILE: (.+) ===$') {
            if ($currentFile) {
                $dir = Split-Path -Parent $currentFile
                if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
                $buffer -join "`n" | Set-Content -LiteralPath $currentFile -Encoding UTF8
                Write-Host "Wrote: $currentFile"
                $buffer = @()
            }
            $rel = $Matches[1]
            $currentFile = Join-Path $outPath $rel
            continue
        }
        if ($currentFile) { $buffer += $line } else { }
    }
    if ($currentFile) {
        $dir = Split-Path -Parent $currentFile
        if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        $buffer -join "`n" | Set-Content -LiteralPath $currentFile -Encoding UTF8
        Write-Host "Wrote: $currentFile"
    }
}

Write-Host "Done. Review changes, then commit locally with:`n  git add .`n  git commit -m 'Apply workspace patch from assistant'"