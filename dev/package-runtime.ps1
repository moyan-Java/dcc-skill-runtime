param(
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$project = Get-Content -LiteralPath (Join-Path $repoRoot "pyproject.toml") -Raw
if ($project -notmatch '(?m)^version\s*=\s*"([^"]+)"') {
    throw "Cannot read package version from pyproject.toml"
}
$version = $Matches[1]

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $repoRoot "dist\runtime-package-$version"
}
$outputPath = [System.IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $outputPath) {
    throw "Output already exists; choose another -OutputDirectory: $outputPath"
}

$sourcePackage = Join-Path $repoRoot "src\dcc_skill_runtime"
$requirements = Join-Path $repoRoot "runtime-requirements.txt"
if (-not (Test-Path -LiteralPath (Join-Path $sourcePackage "__init__.py"))) {
    throw "Runtime Python package is missing: $sourcePackage"
}
if (-not (Test-Path -LiteralPath $requirements)) {
    throw "Runtime requirements are missing: $requirements"
}

$runtimePayload = Join-Path $outputPath "runtime"
$packageTarget = Join-Path $runtimePayload "dcc_skill_runtime"
New-Item -ItemType Directory -Path $packageTarget -Force | Out-Null
Get-ChildItem -LiteralPath $sourcePackage -File -Recurse -Filter "*.py" | ForEach-Object {
    $relativePath = [System.IO.Path]::GetRelativePath($sourcePackage, $_.FullName)
    $targetFile = Join-Path $packageTarget $relativePath
    $targetDirectory = Split-Path -Parent $targetFile
    New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
    Copy-Item -LiteralPath $_.FullName -Destination $targetFile
}
Copy-Item -LiteralPath $requirements -Destination (Join-Path $runtimePayload "requirements.txt")
Copy-Item -LiteralPath (Join-Path $repoRoot "README.md") -Destination (Join-Path $outputPath "README.md")

$archive = "$outputPath.zip"
Compress-Archive -Path (Join-Path $outputPath "*") -DestinationPath $archive

Write-Output "Runtime package: $outputPath"
Write-Output "Archive: $archive"
