#Requires -Version 5.1
# Native Windows CI packages the compiler it has just executed. Every
# archive remains a CI preview; this script publishes no release.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Compiler,
    [Parameter(Mandatory = $true)][ValidateSet('windows-x86_64', 'windows-aarch64')][string]$Target,
    [Parameter(Mandatory = $true)][string]$Output
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$output = [IO.Path]::GetFullPath($Output)
[void][IO.Directory]::CreateDirectory($output)
& python (Join-Path $root 'tests/platform/windows-package.py')
if ($LASTEXITCODE -ne 0) { throw 'Windows package inventory tests failed.' }
& (Join-Path $root 'tests/platform/windows-install.ps1') -Root $root
# The built-in Windows PowerShell checks the documented minimum too.
if (Get-Command powershell.exe -ErrorAction SilentlyContinue) {
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'tests/platform/windows-install.ps1') -Root $root
    if ($LASTEXITCODE -ne 0) { throw 'Windows PowerShell 5.1 installer contracts failed.' }
}
$commit = (& git -C $root rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Could not identify the preview source commit.' }
$version = [IO.File]::ReadAllText((Join-Path $root 'VERSION')).Trim()
& python (Join-Path $root 'scripts/package-windows.py') --root $root --compiler $Compiler --target $Target --output $output --commit $commit
if ($LASTEXITCODE -ne 0) { throw 'Could not assemble the Windows preview archive.' }
$archive = Join-Path $output "axiom-$version-$Target.zip"
$prefix = Join-Path $output 'installation with spaces'
& (Join-Path $root 'scripts/install.ps1') -Archive $archive -Prefix $prefix -ExpectedCommit $commit
if (-not (Test-Path -LiteralPath (Join-Path $prefix 'bin/axiom.exe'))) { throw 'Installer did not place bin/axiom.exe.' }
[IO.File]::WriteAllText((Join-Path $prefix 'keep-user-file.txt'), 'preserve unrelated files')
& (Join-Path $root 'scripts/install.ps1') -Archive $archive -Prefix $prefix -ExpectedCommit $commit
if ([IO.File]::ReadAllText((Join-Path $prefix 'keep-user-file.txt')) -cne 'preserve unrelated files') { throw 'Upgrade changed an unmanaged file.' }
Write-Output "Windows preview package and staged/installed compiler probes EXECUTED on $Target; upgrade preserved unrelated files."
