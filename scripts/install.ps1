#Requires -Version 5.1
<# Install a locally supplied, checksummed Windows preview archive.
   No release is fetched, PATH is not changed, and administrator access
   is not needed. Dot-source this file to load its testable operations. #>
[CmdletBinding()]
param(
    [string]$Archive,
    [string]$Prefix = (Join-Path $HOME '.axiom'),
    [string]$ChecksumPath,
    [string]$ExpectedCommit
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:AxiomManaged = @('bin', 'stdlib', 'docs', 'LICENSE', 'README.md', 'CHANGELOG.md', 'package.json', 'SHA256SUMS')
$script:AxiomUtf8 = New-Object System.Text.UTF8Encoding($false)

function Get-AxiomHostTarget {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'install.ps1 installs Windows preview archives on Windows. Use scripts/install.sh on POSIX hosts.'
    }
    $architecture = [Environment]::GetEnvironmentVariable('PROCESSOR_ARCHITEW6432')
    if (-not $architecture) { $architecture = [Environment]::GetEnvironmentVariable('PROCESSOR_ARCHITECTURE') }
    switch ($architecture) {
        'ARM64' { return 'windows-aarch64' }
        'AMD64' { return 'windows-x86_64' }
        default { throw "Unsupported Windows architecture '$architecture'; a 64-bit host is required." }
    }
}

function Assert-AxiomToolchain {
    foreach ($tool in @('llc', 'lld-link', 'llvm-dlltool')) {
        if (-not (Get-Command $tool -CommandType Application -ErrorAction SilentlyContinue)) {
            throw "LLVM tool '$tool' is missing from PATH. Install LLVM with llc, lld-link and llvm-dlltool before installing Axiom."
        }
    }
    $banner = Invoke-AxiomNative 'llc' @('--version')
    if ($banner.Code -ne 0 -or $banner.Text -notmatch 'LLVM version ([0-9]+)' -or [int]$Matches[1] -lt 18) {
        throw 'llc must run and report LLVM 18 or newer.'
    }
}

function Assert-AxiomNoLinks([string]$Path) {
    $cursor = [IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing a symbolic link or junction in installation path '$cursor'."
            }
            if (-not $item.PSIsContainer) { throw "Installation path '$cursor' is not a directory." }
        }
        $parent = [IO.Path]::GetDirectoryName($cursor.TrimEnd([char[]]'\/'))
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
}

function Resolve-AxiomPrefix([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path) -or $Path -match '[\r\n\x00]' -or -not [IO.Path]::IsPathRooted($Path)) {
        throw 'Prefix must be an absolute directory path without newlines.'
    }
    $normalised = [IO.Path]::GetFullPath($Path)
    $full = $normalised.TrimEnd([char[]]'\/')
    $root = [IO.Path]::GetPathRoot($normalised).TrimEnd([char[]]'\/')
    if ($full -eq $root) { throw 'Prefix must not be a filesystem root.' }
    Assert-AxiomNoLinks $full
    foreach ($protected in @($HOME, $env:USERPROFILE, $env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:ProgramData, $env:SystemRoot)) {
        if (-not $protected) { continue }
        $normal = [IO.Path]::GetFullPath($protected).TrimEnd([char[]]'\/')
        if ($full.Equals($normal, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing protected installation directory '$Path'. Choose a directory of its own."
        }
        if ($protected -eq $env:SystemRoot -and $full.StartsWith($normal + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Prefix must not be inside the Windows system directory.'
        }
    }
    if (Test-Path -LiteralPath (Join-Path $full '.git')) { throw 'Prefix is a git checkout; choose a separate installation directory.' }
    return $full
}

function Get-AxiomFiles([string]$Tree) {
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($Tree)
    while ($pending.Count) {
        $directory = $pending.Pop()
        foreach ($entry in Get-ChildItem -LiteralPath $directory -Force) {
            if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Package or installation contains a link: $($entry.FullName)" }
            if ($entry.PSIsContainer) { $pending.Push($entry.FullName) }
            else { $entry.FullName.Substring($Tree.Length + 1).Replace('\', '/') }
        }
    }
}

function Expand-AxiomArchive([string]$Zip, [string]$Destination, [string]$Name) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($Zip)
    try {
        $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
        [long]$total = 0
        if ($archive.Entries.Count -gt 10000) { throw 'Archive contains too many entries.' }
        foreach ($entry in $archive.Entries) {
            $relative = $entry.FullName
            $parts = $relative.TrimEnd('/').Split('/')
            if ($relative.Contains('\') -or $relative -match '[:\x00-\x1f]' -or $parts[0] -cne $Name -or $parts.Count -lt 2 -or
                @($parts | Where-Object { $_ -eq '' -or $_ -eq '.' -or $_ -eq '..' -or $_.TrimEnd(' ', '.') -cne $_ -or $_ -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])([.].*)?$' }).Count) {
                throw "Archive contains an unsafe path '$relative'."
            }
            if (-not $seen.Add($relative.TrimEnd('/'))) { throw "Archive contains a duplicate or case-colliding path '$relative'." }
            if ((($entry.ExternalAttributes -shr 16) -band 61440) -eq 40960 -or ($entry.ExternalAttributes -band 1024)) {
                throw "Archive contains a symbolic link '$relative'."
            }
            $total += $entry.Length
            if ($entry.Length -gt 268435456 -or $total -gt 1073741824) { throw 'Archive exceeds the installation size limit.' }
            $path = Join-Path $Destination $relative
            if ($relative.EndsWith('/')) { [void][IO.Directory]::CreateDirectory($path); continue }
            [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($path))
            $input = $entry.Open()
            try {
                $output = [IO.File]::Open($path, [IO.FileMode]::CreateNew)
                try { $input.CopyTo($output) } finally { $output.Dispose() }
            } finally { $input.Dispose() }
        }
    } finally { $archive.Dispose() }
}

function Test-AxiomPackage([string]$Tree, [string]$Version, [string]$Target, [string]$Commit) {
    foreach ($required in @('bin/axiom.exe', 'stdlib/Pre.ax', 'docs/reference.md', 'LICENSE', 'README.md', 'CHANGELOG.md', 'package.json', 'SHA256SUMS')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Tree $required) -PathType Leaf)) { throw "Archive is missing '$required'." }
    }
    $metadata = [IO.File]::ReadAllText((Join-Path $Tree 'package.json')) | ConvertFrom-Json
    if ($metadata.format -ne 1 -or $metadata.version -cne $Version -or $metadata.target -cne $Target -or $metadata.commit -notmatch '^[0-9a-f]{40}$') {
        throw 'Package provenance does not match the archive name or format.'
    }
    if ($Commit -and $metadata.commit -cne $Commit) { throw "Package commit $($metadata.commit) does not match expected commit $Commit." }
    $record = @{}
    foreach ($line in [IO.File]::ReadAllLines((Join-Path $Tree 'SHA256SUMS'))) {
        if ($line -notmatch '^([0-9a-f]{64})  ([^\r\n]+)$') { throw 'Malformed package SHA256SUMS.' }
        $digest = $Matches[1]; $path = $Matches[2]
        if ($record.ContainsKey($path)) { throw "Duplicate checksum entry '$path'." }
        $record[$path] = $digest
    }
    $files = @(Get-AxiomFiles $Tree)
    foreach ($path in $files) {
        $top = $path.Split('/')[0]
        if ($script:AxiomManaged -cnotcontains $top) { throw "Unexpected package file '$path'." }
        if ($path -eq 'SHA256SUMS') { continue }
        if (-not $record.ContainsKey($path)) { throw "Package checksum is missing '$path'." }
        if ((Get-FileHash -LiteralPath (Join-Path $Tree $path) -Algorithm SHA256).Hash.ToLowerInvariant() -cne $record[$path]) {
            throw "Package checksum mismatch for '$path'."
        }
        $record.Remove($path)
    }
    if ($record.Count) { throw 'Package checksum inventory names missing files.' }
    return $metadata
}

function Assert-AxiomOwnership([string]$Destination) {
    $record = @{}
    $marker = Join-Path $Destination '.axiom-install'
    if (Test-Path -LiteralPath $marker) {
        if (((Get-Item -LiteralPath $marker -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Ownership record must not be a symbolic link.' }
        $lines = [IO.File]::ReadAllLines($marker)
        if (-not $lines.Count -or $lines[0] -cne 'axiom-install 1') { throw 'Unrecognised .axiom-install ownership record.' }
        foreach ($line in $lines) { if ($line.StartsWith('file ')) { $record[$line.Substring(5)] = $true } }
    }
    foreach ($name in $script:AxiomManaged) {
        $path = Join-Path $Destination $name
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $item = Get-Item -LiteralPath $path -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Managed path '$name' is a link." }
        $files = @($name)
        if ($item.PSIsContainer) { $files = @(Get-AxiomFiles $path | ForEach-Object { "$name/$_" }) }
        foreach ($file in $files) {
            if (-not $record.ContainsKey($file)) { throw "Refusing to replace '$file': no earlier installation owns it." }
        }
    }
}

function Invoke-AxiomNative([string]$Command, [string[]]$Arguments) {
    # Windows PowerShell can turn redirected stderr into error records.
    # Collect both streams and judge native commands by their status;
    # the executable probe's expected success status is 42.
    $PSNativeCommandUseErrorActionPreference = $false
    $ErrorActionPreference = 'Continue'
    $lines = @(& $Command @Arguments 2>&1)
    $code = $LASTEXITCODE
    $global:LASTEXITCODE = 0
    return [PSCustomObject]@{ Code = $code; Text = ($lines -join "`n") }
}

function Test-AxiomCompiler([string]$Tree, [string]$Version, [string]$Work) {
    $probe = Join-Path $Work ([Guid]::NewGuid().ToString('N'))
    [void][IO.Directory]::CreateDirectory($probe)
    [IO.File]::WriteAllText((Join-Path $probe 'probe.ax'), @'
(import IO)
(:: main Int)
;@axiom:effect(io)
(fn (main) { (println "the standard library travelled with the compiler") 42 })
'@, $script:AxiomUtf8)
    $oldPath = $env:PATH; $oldStdlib = $env:AXIOM_STDLIB; $oldModules = $env:AXIOM_PATH
    Push-Location $probe
    try {
        $env:PATH = (Join-Path $Tree 'bin') + [IO.Path]::PathSeparator + $oldPath
        Remove-Item Env:AXIOM_STDLIB, Env:AXIOM_PATH -ErrorAction SilentlyContinue
        $banner = Invoke-AxiomNative 'axiom.exe' @('version')
        if ($banner.Code -ne 0 -or $banner.Text -notmatch ('^Axiom ' + [regex]::Escape($Version) + ' ')) { throw 'Staged compiler version does not match its package.' }
        $built = Invoke-AxiomNative 'axiom.exe' @('--diagnostic-format=ai', 'build', '--input', 'probe.ax', '--output', 'probe.exe')
        if ($built.Code -ne 0) { throw "Compiler cannot build against its packaged standard library: $($built.Text)" }
        $answer = Invoke-AxiomNative (Join-Path $probe 'probe.exe') @()
        if ($answer.Code -ne 42 -or $answer.Text -cne 'the standard library travelled with the compiler') { throw 'Packaged compiler produced the wrong probe result.' }
    } finally {
        Pop-Location
        $env:PATH = $oldPath; $env:AXIOM_STDLIB = $oldStdlib; $env:AXIOM_PATH = $oldModules
    }
}

function Move-AxiomItem([string]$Source, [string]$Destination) {
    Move-Item -LiteralPath $Source -Destination $Destination -ErrorAction Stop
}

function Install-AxiomArchive([string]$ArchivePath, [string]$Destination, [string]$Checksum, [string]$Commit) {
    $target = Get-AxiomHostTarget
    Assert-AxiomToolchain
    $destination = Resolve-AxiomPrefix $Destination
    $archivePath = (Get-Item -LiteralPath $ArchivePath).FullName
    $leaf = [IO.Path]::GetFileName($archivePath)
    if ($leaf -notmatch '^axiom-([0-9]+\.[0-9]+\.[0-9]+)-(windows-(?:x86_64|aarch64))\.zip$') { throw 'Archive name must be axiom-VERSION-windows-ARCH.zip.' }
    $version = $Matches[1]; $packageTarget = $Matches[2]
    if ($packageTarget -cne $target) { throw "Archive targets $packageTarget, but this host is $target." }
    if ($Commit -and $Commit -notmatch '^[0-9a-f]{40}$') { throw 'ExpectedCommit must be a complete lowercase Git commit hash.' }
    if (-not $Checksum) { $Checksum = "$archivePath.sha256" }
    $line = [IO.File]::ReadAllText((Get-Item -LiteralPath $Checksum).FullName).Trim()
    if ($line -notmatch '^([0-9a-fA-F]{64})  ([^\r\n]+)$' -or $Matches[2] -cne $leaf) { throw 'Archive checksum must name this ZIP exactly.' }
    $wanted = $Matches[1].ToLowerInvariant()
    if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant() -cne $wanted) { throw 'Archive checksum mismatch; nothing was installed.' }
    $work = Join-Path ([IO.Path]::GetTempPath()) ('axiom-install-' + [Guid]::NewGuid().ToString('N'))
    $stage = $null; $aside = $null; $locked = $false; $preserveAside = $false
    $lock = Join-Path $destination '.axiom-install.lock'
    [void][IO.Directory]::CreateDirectory($work)
    try {
        $name = [IO.Path]::GetFileNameWithoutExtension($leaf)
        Expand-AxiomArchive $archivePath $work $name
        $tree = Join-Path $work $name
        $metadata = Test-AxiomPackage $tree $version $target $Commit
        [void][IO.Directory]::CreateDirectory($destination)
        [void](New-Item -ItemType Directory -Path $lock -ErrorAction Stop)
        $locked = $true
        Assert-AxiomOwnership $destination
        $stage = Join-Path $destination ('.axiom-stage-' + [Guid]::NewGuid().ToString('N'))
        Copy-Item -LiteralPath $tree -Destination $stage -Recurse
        Test-AxiomCompiler $stage $version $work
        $record = @('axiom-install 1', "version $version", "target $target") + @(Get-AxiomFiles $stage | Sort-Object | ForEach-Object { "file $_" })
        [IO.File]::WriteAllLines((Join-Path $stage '.axiom-install'), [string[]]$record, $script:AxiomUtf8)
        $aside = Join-Path $destination ('.axiom-old-' + [Guid]::NewGuid().ToString('N'))
        [void][IO.Directory]::CreateDirectory($aside)
        $old = New-Object 'System.Collections.Generic.List[string]'
        $fresh = New-Object 'System.Collections.Generic.List[string]'
        try {
            foreach ($name in @($script:AxiomManaged) + @('.axiom-install')) {
                $path = Join-Path $destination $name
                if (Test-Path -LiteralPath $path) { $old.Add($name); Move-AxiomItem $path (Join-Path $aside $name) }
            }
            foreach ($name in @($script:AxiomManaged) + @('.axiom-install')) {
                $path = Join-Path $stage $name
                if (Test-Path -LiteralPath $path) { $fresh.Add($name); Move-AxiomItem $path (Join-Path $destination $name) }
            }
            Test-AxiomCompiler $destination $version $work
        } catch {
            $failure = $_
            try {
                foreach ($name in $fresh) {
                    $path = Join-Path $destination $name
                    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
                }
                foreach ($name in $old) {
                    $path = Join-Path $aside $name
                    if (Test-Path -LiteralPath $path) { Move-AxiomItem $path (Join-Path $destination $name) }
                }
            } catch {
                $preserveAside = $true
                throw "Rollback could not finish. Earlier files remain at '$aside'. Original failure: $failure. Restore failure: $_"
            }
            throw "Installation failed; the previous installation was restored. $failure"
        }
        Write-Output "Axiom $version ($target), package commit $($metadata.commit), installed to $destination"
        Write-Output ('Add its bin directory to PATH: $env:PATH = ' + "'$(Join-Path $destination 'bin');'" + ' + $env:PATH')
    } finally {
        if ($stage -and (Test-Path -LiteralPath $stage)) { Remove-Item -LiteralPath $stage -Recurse -Force }
        if ($aside -and -not $preserveAside -and (Test-Path -LiteralPath $aside)) { Remove-Item -LiteralPath $aside -Recurse -Force }
        if ($locked) { Remove-Item -LiteralPath $lock -Recurse -Force }
        Remove-Item -LiteralPath $work -Recurse -Force
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    if (-not $Archive) { throw 'Supply -Archive axiom-VERSION-windows-ARCH.zip and its .sha256 sidecar. Windows packages are CI preview artifacts; no Windows release archive is published.' }
    Install-AxiomArchive $Archive $Prefix $ChecksumPath $ExpectedCommit
}
