[CmdletBinding()]
param(
    [string]$BuildDirectory = "",
    [string]$OutputPath = "",
    [string]$Generator = "",
    [string]$CMake = "cmake"
)

$ErrorActionPreference = "Stop"
$sourceDirectory = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$cmakeCommand = Get-Command $CMake -ErrorAction SilentlyContinue
if (-not $cmakeCommand) {
    throw "CMake was not found. Install CMake 3.15 or newer and make it available on PATH."
}

# Do not let a persistent CMakeCache switch silently between MSVC and MinGW.
# CI enters the MSVC developer environment; the local developer checkout has
# w64devkit on PATH.  An explicit -Generator always wins.  Do not infer a
# particular Visual Studio release from the presence of ``cl``: hosted
# runners can move between VS 17 and VS 18.
$compilerFlavor = "default"
if (Get-Command cl -ErrorAction SilentlyContinue) {
    $compilerFlavor = "msvc"
} elseif (Get-Command mingw32-make -ErrorAction SilentlyContinue) {
    $compilerFlavor = "mingw"
}
if (-not $Generator -and $env:CMAKE_GENERATOR) {
    $Generator = $env:CMAKE_GENERATOR
}
if (-not $Generator) {
    if ($compilerFlavor -eq "mingw") {
        $Generator = "MinGW Makefiles"
    } elseif ($compilerFlavor -eq "msvc" -and (Get-Command nmake -ErrorAction SilentlyContinue)) {
        $Generator = "NMake Makefiles"
    }
}
if (-not $BuildDirectory) {
    $buildFlavor = if ($Generator -match "Visual Studio|NMake") { "msvc" } elseif ($Generator -match "MinGW|MSYS|Ninja") { "mingw" } else { $compilerFlavor }
    $BuildDirectory = Join-Path $PSScriptRoot "..\..\runtime\build\spine-converter-$buildFlavor"
}
if (-not $OutputPath) {
    $OutputPath = Join-Path $PSScriptRoot "..\..\runtime\tools\SpineSkeletonDataConverter\lpk_spine_converter.dll"
}
$buildDirectory = [IO.Path]::GetFullPath($BuildDirectory)
$outputFile = [IO.Path]::GetFullPath($OutputPath)

New-Item -ItemType Directory -Force -Path $buildDirectory | Out-Null
New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($outputFile)) | Out-Null

$configureArgs = @("-S", $sourceDirectory, "-B", $buildDirectory, "-DCMAKE_BUILD_TYPE=Release")
if ($Generator) {
    $configureArgs += @("-G", $Generator)
    if ($Generator -match "Visual Studio") {
        $configureArgs += @("-A", "x64")
    }
}
& $CMake @configureArgs
if ($LASTEXITCODE -ne 0) {
    throw "CMake configure failed with exit code $LASTEXITCODE."
}

& $CMake --build $buildDirectory --config Release --target lpk_spine_converter --parallel
if ($LASTEXITCODE -ne 0) {
    throw "Native Spine converter build failed with exit code $LASTEXITCODE."
}

$candidates = @(
    (Join-Path $buildDirectory "lpk_spine_converter.dll"),
    (Join-Path $buildDirectory "Release\lpk_spine_converter.dll")
)
$artifact = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $artifact) {
    $artifact = Get-ChildItem -LiteralPath $buildDirectory -Filter "lpk_spine_converter.dll" -File -Recurse |
        Select-Object -ExpandProperty FullName -First 1
}
if (-not $artifact) {
    throw "CMake completed but lpk_spine_converter.dll was not found under $buildDirectory."
}

Copy-Item -LiteralPath $artifact -Destination $outputFile -Force
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $outputFile).Hash.ToLowerInvariant()
Write-Output "Installed $outputFile"
Write-Output "SHA-256 $hash"
