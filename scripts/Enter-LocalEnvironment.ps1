# Dot-source from PowerShell: . ./scripts/Enter-LocalEnvironment.ps1
# Uses the user-scoped tools installed for this checkout; changes only this shell.
$projectRoot = Split-Path -Parent $PSScriptRoot
$toolDirectories = @(
    (Join-Path $projectRoot '.venv/Scripts'),
    (Join-Path $env:USERPROFILE '.local/share/node/node-v22.23.2-win-x64'),
    (Join-Path $env:APPDATA 'npm/node_modules/bun/bin'),
    (Join-Path $env:USERPROFILE '.local/bin')
)
foreach ($toolDirectory in $toolDirectories) {
    if (-not (Test-Path -LiteralPath $toolDirectory -PathType Container)) {
        throw "Missing local tool directory: $toolDirectory. See docs/LOCAL_SETUP.md."
    }
}
$env:PATH = ($toolDirectories -join [IO.Path]::PathSeparator) + [IO.Path]::PathSeparator + $env:PATH
Write-Host 'Local Python, Node 22, Bun and RTK are available in this shell.'
