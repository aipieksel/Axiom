#Requires -Version 5.1
# Filesystem contract tests run on PowerShell on any host. Native
# compiler execution is covered by scripts/check-windows-install.ps1.
param([string]$Root = (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent))
$ErrorActionPreference = 'Stop'
. (Join-Path $Root 'scripts/install.ps1')
$testRoot = Join-Path $Root ('.axiom-shared/install-test-' + [Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($testRoot)
$script:checks = 0
$script:probes = 0
$script:failProbe = 0
$script:moves = 0
$script:failMove = 0
$commit = 'a' * 40
$name = 'axiom-9.9.9-windows-x86_64'

function Check([bool]$Condition, [string]$Label) {
    if (-not $Condition) { throw "FAIL $Label" }
    $script:checks++
    Write-Output "ok   $Label"
}
function Refuses([scriptblock]$Operation, [string]$Pattern, [string]$Label) {
    $message = ''
    try { & $Operation | Out-Null } catch { $message = $_.ToString() }
    Check ($message -match $Pattern) "$Label ($message)"
}
function Write-TestFile([string]$Path, [string]$Text) {
    [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Path))
    [IO.File]::WriteAllText($Path, $Text, $script:AxiomUtf8)
}
function Make-Package([string]$Label) {
    $folder = Join-Path $testRoot ('build-' + [Guid]::NewGuid().ToString('N'))
    $tree = Join-Path $folder $name
    foreach ($file in @('bin/axiom.exe', 'stdlib/Pre.ax', 'docs/reference.md', 'LICENSE', 'README.md', 'CHANGELOG.md')) {
        Write-TestFile (Join-Path $tree $file) "$Label $file"
    }
    Write-TestFile (Join-Path $tree 'package.json') ('{"format":1,"version":"9.9.9","target":"windows-x86_64","commit":"' + $commit + '"}')
    $lines = @(Get-AxiomFiles $tree | Sort-Object | ForEach-Object {
        (Get-FileHash -LiteralPath (Join-Path $tree $_) -Algorithm SHA256).Hash.ToLowerInvariant() + '  ' + $_
    })
    [IO.File]::WriteAllLines((Join-Path $tree 'SHA256SUMS'), [string[]]$lines, $script:AxiomUtf8)
    return $folder
}
function Zip-Package([string]$Folder) {
    $directory = Join-Path $testRoot ('archive-' + [Guid]::NewGuid().ToString('N'))
    [void][IO.Directory]::CreateDirectory($directory)
    $zip = Join-Path $directory ($name + '.zip')
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    # Framework compatibility modes can make CreateFromDirectory write
    # backslashes. Use the same canonical member names as package-windows.py.
    $members = @(Get-AxiomFiles $Folder | Sort-Object)
    $stream = [IO.Compression.ZipFile]::Open($zip, [IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($member in $members) {
            [void][IO.Compression.ZipFileExtensions]::CreateEntryFromFile($stream, (Join-Path $Folder $member), $member)
        }
    } finally { $stream.Dispose() }
    $stream = [IO.Compression.ZipFile]::OpenRead($zip)
    try {
        $actual = @($stream.Entries | ForEach-Object { $_.FullName } | Sort-Object)
        if (($actual -join "`n") -cne ($members -join "`n")) { throw 'Fixture ZIP member names differ from the package inventory.' }
    } finally { $stream.Dispose() }
    Update-Sum $zip
    return $zip
}
function Update-Sum([string]$Zip) {
    $line = (Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash.ToLowerInvariant() + '  ' + [IO.Path]::GetFileName($Zip) + "`n"
    [IO.File]::WriteAllText("$Zip.sha256", $line, $script:AxiomUtf8)
}
function Snapshot([string]$Folder) {
    return (@(Get-AxiomFiles $Folder | Sort-Object | ForEach-Object {
        $_ + ':' + (Get-FileHash -LiteralPath (Join-Path $Folder $_) -Algorithm SHA256).Hash
    }) -join "`n")
}

# These two boundaries isolate filesystem installation from the host OS
# and executable probe. Production entry never offers such switches.
function Get-AxiomHostTarget { return 'windows-x86_64' }
function Assert-AxiomToolchain { }
function Test-AxiomCompiler([string]$Tree, [string]$Version, [string]$Work) {
    $script:probes++
    if ($script:probes -eq $script:failProbe) { throw 'injected compiler refusal' }
    if (-not (Test-Path -LiteralPath (Join-Path $Tree 'stdlib/Pre.ax'))) { throw 'probe has no standard library' }
}
function Move-AxiomItem([string]$Source, [string]$Destination) {
    $script:moves++
    if ($script:moves -eq $script:failMove) { throw 'injected rename refusal' }
    Move-Item -LiteralPath $Source -Destination $Destination -ErrorAction Stop
}

try {
    $zip = Zip-Package (Make-Package 'first')
    Check $true 'fixture ZIP members match the canonical package inventory'
    $prefix = Join-Path $testRoot 'installation with spaces'
    Install-AxiomArchive $zip $prefix '' $commit | Out-Null
    Check ((Test-Path -LiteralPath (Join-Path $prefix 'bin/axiom.exe')) -and $script:probes -eq 2) 'staged and installed layouts are probed'
    Check (([IO.File]::ReadAllLines((Join-Path $prefix '.axiom-install')))[0] -eq 'axiom-install 1') 'ownership record is written'
    Write-TestFile (Join-Path $prefix 'user-note.txt') 'keep this unmanaged file'
    $before = Snapshot $prefix
    $script:probes = 0; $script:failProbe = 1
    Refuses { Install-AxiomArchive $zip $prefix '' $commit } 'injected compiler refusal' 'staged compiler refusal preserves installation'
    Check ((Snapshot $prefix) -ceq $before) 'staged refusal leaves every old file unchanged'
    $script:probes = 0; $script:failProbe = 2
    Refuses { Install-AxiomArchive $zip $prefix '' $commit } 'previous installation was restored' 'post-switch refusal rolls back'
    Check ((Snapshot $prefix) -ceq $before) 'rollback restores compiler, library, provenance and ownership'
    $script:failProbe = 0; $script:moves = 0; $script:failMove = 11
    Refuses { Install-AxiomArchive $zip $prefix '' $commit } 'previous installation was restored' 'rename refusal rolls back'
    Check ((Snapshot $prefix) -ceq $before) 'rename rollback keeps old and unmanaged files'
    $script:failMove = 0
    $upgrade = Zip-Package (Make-Package 'upgrade')
    Install-AxiomArchive $upgrade $prefix '' $commit | Out-Null
    Check ([IO.File]::ReadAllText((Join-Path $prefix 'bin/axiom.exe')).StartsWith('upgrade')) 'owned files can be upgraded'
    Check ([IO.File]::ReadAllText((Join-Path $prefix 'user-note.txt')) -ceq 'keep this unmanaged file') 'upgrade preserves unrelated prefix files'
    Write-TestFile (Join-Path $prefix 'bin/foreign.exe') 'not ours'
    Refuses { Install-AxiomArchive $zip $prefix '' $commit } 'no earlier installation owns' 'unrecorded files cannot be overwritten'
    Check (Test-Path -LiteralPath (Join-Path $prefix 'bin/foreign.exe')) 'unrecorded file survives refusal'
    Refuses { Install-AxiomArchive $zip (Join-Path $testRoot 'wrong-commit') '' ('b' * 40) } 'does not match expected commit' 'expected source commit is enforced'
    Refuses { Resolve-AxiomPrefix $HOME } 'protected' 'home is protected'
    Refuses { Resolve-AxiomPrefix (Join-Path $HOME '.') } 'protected' 'home alias is protected'
    Refuses { Resolve-AxiomPrefix ([IO.Path]::GetPathRoot($testRoot)) } 'filesystem root' 'filesystem root is protected'
    Refuses { Resolve-AxiomPrefix 'relative-directory' } 'absolute' 'relative prefix is refused'
    $checkout = Join-Path $testRoot 'checkout'
    [void][IO.Directory]::CreateDirectory((Join-Path $checkout '.git'))
    Refuses { Resolve-AxiomPrefix $checkout } 'git checkout' 'repository prefix is protected'
    $lockPrefix = Join-Path $testRoot 'concurrent'
    [void][IO.Directory]::CreateDirectory((Join-Path $lockPrefix '.axiom-install.lock'))
    Refuses { Install-AxiomArchive $zip $lockPrefix '' $commit } 'already exists' 'concurrent installation lock is respected'
    Check (Test-Path -LiteralPath (Join-Path $lockPrefix '.axiom-install.lock')) 'another installation lock is not removed'
    [IO.File]::AppendAllText($zip, 'x')
    Refuses { Install-AxiomArchive $zip (Join-Path $testRoot 'tampered') '' $commit } 'checksum mismatch' 'tampered archive is refused'
    Update-Sum $zip
    Install-AxiomArchive $zip (Join-Path $testRoot 'rechecked') '' $commit | Out-Null
    Check $true 'same ZIP passes after its actual digest is supplied'
    Remove-Item -LiteralPath "$zip.sha256"
    Refuses { Install-AxiomArchive $zip (Join-Path $testRoot 'no-checksum') '' $commit } 'Cannot find|Could not find|does not exist' 'missing sidecar is refused'
    $folder = Make-Package 'changed'
    Write-TestFile (Join-Path $folder "$name/stdlib/Pre.ax") 'different bytes'
    $badInventory = Zip-Package $folder
    Refuses { Install-AxiomArchive $badInventory (Join-Path $testRoot 'bad-inventory') '' $commit } 'Package checksum mismatch' 'per-file digests are verified'
    $unsafeZip = Join-Path $testRoot 'unsafe.zip'
    $stream = [IO.Compression.ZipFile]::Open($unsafeZip, [IO.Compression.ZipArchiveMode]::Create)
    [void]$stream.CreateEntry("$name/../escape")
    $stream.Dispose()
    Refuses { Expand-AxiomArchive $unsafeZip (Join-Path $testRoot 'extract') $name } 'unsafe path' 'ZIP traversal is refused'
    Remove-Item -LiteralPath $unsafeZip
    $stream = [IO.Compression.ZipFile]::Open($unsafeZip, [IO.Compression.ZipArchiveMode]::Create)
    [void]$stream.CreateEntry("$name\docs\reference.md")
    $stream.Dispose()
    Refuses { Expand-AxiomArchive $unsafeZip (Join-Path $testRoot 'backslash') $name } 'unsafe path' 'backslash ZIP members are refused'
    Remove-Item -LiteralPath $unsafeZip
    $stream = [IO.Compression.ZipFile]::Open($unsafeZip, [IO.Compression.ZipArchiveMode]::Create)
    [void]$stream.CreateEntry("$name/docs\..\escape")
    $stream.Dispose()
    Refuses { Expand-AxiomArchive $unsafeZip (Join-Path $testRoot 'mixed-traversal') $name } 'unsafe path' 'mixed-separator ZIP traversal is refused'
    Remove-Item -LiteralPath $unsafeZip
    $stream = [IO.Compression.ZipFile]::Open($unsafeZip, [IO.Compression.ZipArchiveMode]::Create)
    [void]$stream.CreateEntry("$name/bin/AXIOM.exe")
    [void]$stream.CreateEntry("$name/bin/axiom.exe")
    $stream.Dispose()
    Refuses { Expand-AxiomArchive $unsafeZip (Join-Path $testRoot 'collision') $name } 'case-colliding' 'Windows case collisions are refused'
    Remove-Item -LiteralPath $unsafeZip
    $stream = [IO.Compression.ZipFile]::Open($unsafeZip, [IO.Compression.ZipArchiveMode]::Create)
    $link = $stream.CreateEntry("$name/stdlib/link.ax")
    $link.ExternalAttributes = [int](40960 -shl 16)
    $stream.Dispose()
    Refuses { Expand-AxiomArchive $unsafeZip (Join-Path $testRoot 'link') $name } 'symbolic link' 'archive symlinks are refused'
    Remove-Item -LiteralPath $unsafeZip
    $stream = [IO.Compression.ZipFile]::Open($unsafeZip, [IO.Compression.ZipArchiveMode]::Create)
    [void]$stream.CreateEntry("$name/docs/NUL.md")
    $stream.Dispose()
    Refuses { Expand-AxiomArchive $unsafeZip (Join-Path $testRoot 'reserved') $name } 'unsafe path' 'Windows device filenames are refused'
    Check (-not @(Get-ChildItem -LiteralPath $prefix -Force | Where-Object { $_.Name -match '^\.axiom-(stage|old|install[.]lock)' }).Count) 'completed transactions leave no lock or stage'
    Write-Output "Windows installer filesystem contracts: $script:checks passed (compiler execution mocked; native CI checks it separately)."
} finally {
    Remove-Item -LiteralPath $testRoot -Recurse -Force
}
