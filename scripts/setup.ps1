<#
.SYNOPSIS
Set up this extracted Desktop-MCP checkout on Windows 10/11 x64.
.DESCRIPTION
Run Setup.cmd, or use -WhatIf for a read-only plan. -SkipCopilot and
-SkipShortcut omit those integrations. -CopilotConfig selects another config
file. Nothing opens/arms Desktop-MCP or installs Copilot, a model, or compilers.
Keep this folder in place: the environment, shortcut and client use absolute
paths. A different installation's shortcut/registration is never replaced.
#>
[CmdletBinding()]
param(
    [switch]$WhatIf,
    [switch]$SkipCopilot,
    [switch]$SkipShortcut,
    [string]$CopilotConfig
)

$script:SetupRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:UvVersion = '0.12.10'
$script:UvAsset = 'uv-x86_64-pc-windows-msvc.zip'
# Official release asset and its .sha256, verified 2026-09-06:
# https://github.com/astral-sh/uv/releases/download/0.12.10/uv-x86_64-pc-windows-msvc.zip.sha256
$script:UvSha256 = 'f65744f94072152b1f86ba2aace4d01f1124d9a8ecb235805039e3718c36cac2'
$script:MaxArchiveBytes = 32MB
$script:MaxTreeEntries = 100000
$script:MaxTreeDepth = 64
$script:MaxTreeSeconds = 30
$script:CacheOwner = 'Desktop-MCP setup cache v1'

function Get-SetupPathInfo([string]$Path) {
    try { return (Get-Item -LiteralPath $Path -Force -ErrorAction Stop) }
    catch [System.Management.Automation.ItemNotFoundException] { return $null }
}

function Get-SetupChildPaths([string]$Path) {
    return ,([IO.Directory]::EnumerateFileSystemEntries($Path).GetEnumerator())
}

function Assert-PlainPath([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        $item = Get-SetupPathInfo $current
        if ($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Setup refuses linked/junction paths. Use a normal local installation folder.'
        }
        $current = [IO.Path]::GetDirectoryName($current)
    }
}

function Assert-CacheOwner([string]$Cache) {
    $marker = Join-Path $Cache 'owner.txt'
    Assert-PlainPath $marker
    $info = Get-SetupPathInfo $marker
    if (-not $info -or ($info.Attributes -band [IO.FileAttributes]::Directory) -or
        $info.Length -gt 128 -or [IO.File]::ReadAllText($marker) -ne $script:CacheOwner) {
        throw 'The setup cache lacks the expected ownership marker. It was not changed.'
    }
}

function Resolve-OwnedPythonAlias([string]$Path, $Item, [string]$Cache) {
    $pythonRoot = Join-Path $Cache 'python'
    $alias = Join-Path $pythonRoot 'cpython-3.14-windows-x86_64-none'
    if (-not $Path.Equals($alias, [StringComparison]::OrdinalIgnoreCase) -or
        -not ($Item.Attributes -band [IO.FileAttributes]::Directory) -or
        $Item.LinkType -ne 'Junction') { return $null }
    $targets = @($Item.Target)
    if ($targets.Count -ne 1 -or $targets[0] -isnot [string]) { return $null }
    $targetText = $targets[0].TrimEnd([char]'\')
    $compareRoot = $pythonRoot
    foreach ($prefix in @('\??\', '\\?\')) {
        if ($targetText.StartsWith($prefix, [StringComparison]::Ordinal)) {
            $targetText = $targetText.Substring($prefix.Length)
        }
        if ($compareRoot.StartsWith($prefix, [StringComparison]::Ordinal)) {
            $compareRoot = $compareRoot.Substring($prefix.Length)
        }
    }
    $rootPrefix = $compareRoot + '\'
    if (-not $targetText.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        return $null
    }
    $name = $targetText.Substring($rootPrefix.Length)
    if ($name -notmatch '^cpython-3\.14\.(0|[1-9][0-9]*)-windows-x86_64-none$') {
        return $null
    }
    Assert-CacheOwner $Cache
    Assert-PlainPath $pythonRoot
    $target = Join-Path $pythonRoot $name
    $targetInfo = Get-SetupPathInfo $target
    if (-not $targetInfo -or
        -not ($targetInfo.Attributes -band [IO.FileAttributes]::Directory) -or
        ($targetInfo.Attributes -band [IO.FileAttributes]::ReparsePoint)) { return $null }
    return $target
}

function Assert-InstallTree([string]$Path) {
    Assert-PlainPath $Path
    $cache = Join-Path $script:SetupRoot '.desktop-mcp-setup-cache'
    $allowPythonAlias = [IO.Path]::GetFullPath($Path).Equals($cache, [StringComparison]::OrdinalIgnoreCase)
    $pending = New-Object 'Collections.Generic.Stack[object]'
    $visited = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $pending.Push(@{ Path = $Path; Depth = 0 })
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $count = 1
    while ($pending.Count -gt 0) {
        $next = $pending.Pop()
        if ($watch.Elapsed.TotalSeconds -gt $script:MaxTreeSeconds -or
            $next.Depth -gt $script:MaxTreeDepth) {
            throw 'The install-tree inspection exceeded its time/depth limit. Nothing was synchronized.'
        }
        if (-not $visited.Add($next.Path)) { continue }
        $item = Get-SetupPathInfo $next.Path
        if (-not $item) { continue }
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            $target = $null
            if ($allowPythonAlias) { $target = Resolve-OwnedPythonAlias $next.Path $item $cache }
            if ($target) {
                # uv's one known minor alias is never traversed. Walk its plain
                # sibling target separately, under this same budget and guard.
                if (-not $visited.Contains($target)) {
                    $count += 1
                    if ($count -gt $script:MaxTreeEntries) {
                        throw 'The install-tree inspection exceeded its entry limit. Nothing was synchronized.'
                    }
                    $pending.Push(@{ Path = $target; Depth = $next.Depth })
                }
                continue
            }
            throw 'Setup refuses linked/junction descendants in the environment or managed cache. Nothing was synchronized.'
        }
        if ($item.Attributes -band [IO.FileAttributes]::Directory) {
            # One level only. Inspect each node before enumerating it; never use -Recurse.
            $children = Get-SetupChildPaths $next.Path
            try {
                while ($children.MoveNext()) {
                    $count += 1
                    if ($count -gt $script:MaxTreeEntries -or
                        $watch.Elapsed.TotalSeconds -gt $script:MaxTreeSeconds) {
                        throw 'The install-tree inspection exceeded its entry/time limit. Nothing was synchronized.'
                    }
                    $pending.Push(@{ Path = [string]$children.Current; Depth = $next.Depth + 1 })
                }
            } finally {
                if ($children -is [IDisposable]) { $children.Dispose() }
            }
        }
    }
}

function Assert-SetupRoot {
    foreach ($marker in @('Setup.cmd', 'pyproject.toml', 'uv.lock',
            'src\desktop_mcp\__main__.py', 'src\desktop_mcp\launcher.py',
            'scripts\configure_copilot.py')) {
        if (-not (Test-Path -LiteralPath (Join-Path $script:SetupRoot $marker) -PathType Leaf)) {
            throw "Incomplete Desktop-MCP source folder (missing $marker). Extract the whole ZIP first."
        }
    }
    $project = [IO.File]::ReadAllText((Join-Path $script:SetupRoot 'pyproject.toml'))
    if ($project -notmatch '(?m)^name\s*=\s*"desktop-mcp"\s*$') {
        throw 'This is not the Desktop-MCP project; nothing was installed.'
    }
    Assert-PlainPath $script:SetupRoot
}

function Assert-SetupPlatform {
    $architecture = $env:PROCESSOR_ARCHITEW6432
    if (-not $architecture) { $architecture = $env:PROCESSOR_ARCHITECTURE }
    if ([Environment]::OSVersion.Platform -ne 'Win32NT' -or
        [Environment]::OSVersion.Version.Major -lt 10 -or
        -not [Environment]::Is64BitOperatingSystem -or $architecture -ne 'AMD64') {
        throw 'Setup supports Windows 10/11 x64 only. ARM64/32-bit native dependencies are not supported by this setup.'
    }
}

function Initialize-SetupCache([string]$Cache) {
    Assert-InstallTree $Cache
    $marker = Join-Path $Cache 'owner.txt'
    if (Test-Path -LiteralPath $Cache) {
        Assert-CacheOwner $Cache
    } else {
        [void][IO.Directory]::CreateDirectory($Cache)
        [IO.File]::WriteAllText($marker, $script:CacheOwner)
    }
    foreach ($name in @('packages', 'python', 'python-archives', 'work')) {
        Assert-PlainPath (Join-Path $Cache $name)
    }
    [void][IO.Directory]::CreateDirectory((Join-Path $Cache 'work'))
}

function Copy-BoundedStream($Source, $Destination, [long]$Limit, [int]$Seconds = 120) {
    $buffer = New-Object byte[] 65536
    $watch = [Diagnostics.Stopwatch]::StartNew()
    [long]$total = 0
    while (($count = $Source.Read($buffer, 0, $buffer.Length)) -gt 0) {
        $total += $count
        if ($total -gt $Limit -or $watch.Elapsed.TotalSeconds -gt $Seconds) {
            throw 'The uv download/extraction exceeded its size or time limit.'
        }
        $Destination.Write($buffer, 0, $count)
    }
    return $total
}

function Open-UvDownload {
    $request = [Net.HttpWebRequest]::Create(
        "https://github.com/astral-sh/uv/releases/download/$script:UvVersion/$script:UvAsset")
    $request.Timeout = 30000
    $request.ReadWriteTimeout = 30000
    $request.MaximumAutomaticRedirections = 5
    $request.UserAgent = 'Desktop-MCP-Setup'
    return $request.GetResponse()
}

function Receive-UvArchive([string]$Destination) {
    $protocol = [Net.ServicePointManager]::SecurityProtocol
    $response = $null; $inputStream = $null; $outputStream = $null
    try {
        [Net.ServicePointManager]::SecurityProtocol = $protocol -bor [Net.SecurityProtocolType]::Tls12
        $response = Open-UvDownload
        if ($response.ResponseUri.Scheme -ne 'https' -or
            $response.ContentLength -gt $script:MaxArchiveBytes) {
            throw 'The uv download response is not HTTPS or exceeds its size limit.'
        }
        $inputStream = $response.GetResponseStream()
        $outputStream = [IO.File]::Open($Destination, 'CreateNew', 'Write', 'None')
        [void](Copy-BoundedStream $inputStream $outputStream $script:MaxArchiveBytes)
    } finally {
        if ($outputStream) { $outputStream.Dispose() }
        if ($inputStream) { $inputStream.Dispose() }
        if ($response) { $response.Dispose() }
        [Net.ServicePointManager]::SecurityProtocol = $protocol
    }
}

function Find-UvExecutable {
    $command = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($command) { return $command.Source }
}

function Start-UvVersionProcess($Info) {
    return [Diagnostics.Process]::Start($Info)
}

function Read-UvVersion([string]$Executable) {
    $info = New-SetupProcess $Executable @('--version') $script:SetupRoot
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = Start-UvVersionProcess $info
    try {
        # Wait before reading: oversized output fills the bounded OS pipe and
        # times out instead of being accumulated into an unbounded string.
        if (-not $process.WaitForExit(5000)) {
            Stop-Process -Id $process.Id -ErrorAction Stop
            return $null
        }
        if ($process.ExitCode -ne 0) { return $null }
        $text = $process.StandardOutput.ReadToEnd()
        if ($text.Length -le 512) { return $text.Trim() }
        return $null
    } finally { $process.Dispose() }
}

function Test-UvCompatibility([string]$Executable) {
    try {
        $text = Read-UvVersion $Executable
        if ($text -match '^uv (\d+\.\d+\.\d+)(?:\s|$)') {
            # The verified release is the supported floor, including Python/GIL
            # selection and the process-local no-registry controls used below.
            return ([version]$Matches[1] -ge [version]$script:UvVersion)
        }
    } catch { }
    return $false
}

function Get-UvExecutable([string]$Cache) {
    $existing = Find-UvExecutable
    if ($existing -and (Test-UvCompatibility $existing)) { return $existing }
    if ($existing) {
        Write-Host "Existing uv is incompatible/unavailable; using verified project-local uv $script:UvVersion without changing it."
    }
    $archivePath = Join-Path $Cache "uv-$script:UvVersion.zip"
    Assert-PlainPath $archivePath
    if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
        Write-Host "Downloading official uv $script:UvVersion (Windows x64)..."
        $download = Join-Path $Cache ("uv-" + [Guid]::NewGuid().ToString('N') + '.download')
        Receive-UvArchive $download
        if ((Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash -ne $script:UvSha256) {
            throw 'The uv SHA256 checksum did not match the pinned official release. Nothing was executed.'
        }
        [IO.File]::Move($download, $archivePath)
    }
    if ((Get-Item -LiteralPath $archivePath).Length -gt $script:MaxArchiveBytes -or
        (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash -ne $script:UvSha256) {
        throw 'The cached uv archive failed SHA256/size verification. Nothing was executed.'
    }
    $executable = Join-Path $Cache "uv-$script:UvVersion.exe"
    Assert-PlainPath $executable
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($archivePath)
    try {
        $entries = @($archive.Entries | Where-Object { $_.FullName -ceq 'uv.exe' })
        if ($archive.Entries.Count -gt 8 -or $entries.Count -ne 1 -or
            $entries[0].Length -le 0 -or $entries[0].Length -gt 128MB) {
            throw 'The verified uv archive has an unexpected executable layout or size.'
        }
        # Never expand archive-controlled paths; only copy uv.exe to our exact owned path.
        $source = $entries[0].Open()
        try {
            $destination = [IO.File]::Open($executable, 'Create', 'Write', 'None')
            try {
                $length = Copy-BoundedStream $source $destination 128MB
                if ($length -ne $entries[0].Length) { throw 'The uv executable is incomplete.' }
            } finally { $destination.Dispose() }
        } finally { $source.Dispose() }
    } finally { $archive.Dispose() }
    return $executable
}

function New-SetupProcess([string]$Executable, [string[]]$Arguments, [string]$Root) {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $Executable
    # Windows argv quoting; no shell interpolation, including spaces and trailing backslashes.
    $quoted = foreach ($argument in $Arguments) {
        '"' + [regex]::Replace(
            [regex]::Replace($argument, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
    }
    $info.Arguments = $quoted -join ' '
    $info.WorkingDirectory = $Root
    $info.UseShellExecute = $false
    foreach ($name in @($info.EnvironmentVariables.Keys)) {
        if ($name -like 'UV_*' -or $name -in @('VIRTUAL_ENV', 'CONDA_PREFIX', 'PYTHONHOME', 'PYTHONPATH')) {
            $info.EnvironmentVariables.Remove($name)
        }
    }
    $cache = Join-Path $Root '.desktop-mcp-setup-cache'
    $info.EnvironmentVariables['UV_PROJECT'] = $Root
    $info.EnvironmentVariables['UV_PROJECT_ENVIRONMENT'] = Join-Path $Root '.venv'
    $info.EnvironmentVariables['UV_CACHE_DIR'] = Join-Path $cache 'packages'
    $info.EnvironmentVariables['UV_PYTHON_INSTALL_DIR'] = Join-Path $cache 'python'
    $info.EnvironmentVariables['UV_PYTHON_CACHE_DIR'] = Join-Path $cache 'python-archives'
    $info.EnvironmentVariables['UV_PYTHON_INSTALL_BIN'] = '0'
    $info.EnvironmentVariables['UV_PYTHON_INSTALL_REGISTRY'] = '0'
    $info.EnvironmentVariables['UV_PYTHON_NO_REGISTRY'] = '1'
    $info.EnvironmentVariables['UV_PYTHON_DOWNLOADS'] = 'automatic'
    $info.EnvironmentVariables['UV_HTTP_TIMEOUT'] = '60'
    $info.EnvironmentVariables['TEMP'] = Join-Path $cache 'work'
    $info.EnvironmentVariables['TMP'] = Join-Path $cache 'work'
    return $info
}

function Invoke-SetupProcess([string]$Executable, [string[]]$Arguments, [string]$Root) {
    $process = [Diagnostics.Process]::Start((New-SetupProcess $Executable $Arguments $Root))
    try {
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) {
            $failure = New-Object InvalidOperationException(
                "Setup command failed (exit $($process.ExitCode)). No compiler or system repair was attempted.")
            $failure.Data['ExitCode'] = $process.ExitCode
            throw $failure
        }
    } finally { $process.Dispose() }
}

function Invoke-DesktopSetup([switch]$WhatIf, [switch]$SkipCopilot,
        [switch]$SkipShortcut, [string]$CopilotConfig) {
    Assert-SetupRoot
    Assert-SetupPlatform
    $root = $script:SetupRoot
    $cache = Join-Path $root '.desktop-mcp-setup-cache'
    $venv = Join-Path $root '.venv'
    $python = Join-Path $venv 'Scripts\python.exe'
    if (-not $CopilotConfig) {
        $CopilotConfig = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.copilot\mcp-config.json'
    }
    $CopilotConfig = [IO.Path]::GetFullPath($CopilotConfig)
    Write-Host "Desktop-MCP folder: $root"
    if ($WhatIf) {
        Write-Host "PLAN ONLY: use compatible uv >= $script:UvVersion or verified uv $script:UvVersion in $cache"
        Write-Host 'Inspect the existing environment/cache without following links before running setup processes.'
        Write-Host "Prepare $venv with 64-bit Python 3.14 and uv sync --frozen --inexact."
        if (-not $SkipShortcut) { Write-Host 'Invoke the existing install-shortcut command (never replace a different target).' }
        if (-not $SkipCopilot) { Write-Host "Safely merge desktop-mcp into $CopilotConfig (at least 45000 ms, supervised serve)." }
        Write-Host 'No downloads, processes, configuration reads, or writes were performed.'
        return
    }
    Assert-InstallTree $venv
    Assert-InstallTree $cache
    Assert-PlainPath $python
    $verify = 'import pathlib, sys, sysconfig; assert sys.implementation.name == "cpython" and sys.version_info[:2] == (3, 14) and sys.maxsize > 2**32 and not sysconfig.get_config_var("Py_GIL_DISABLED") and pathlib.Path(sys.prefix).resolve() == pathlib.Path(sys.argv[1]).resolve(), "Expected this folder''s 64-bit GIL-enabled Python 3.14 environment; existing environments are not replaced"'
    if (Test-Path -LiteralPath $venv) {
        if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or
            -not (Test-Path -LiteralPath (Join-Path $venv 'pyvenv.cfg') -PathType Leaf)) {
            throw 'An incomplete/different .venv already exists. It was not replaced. Use a fresh source folder.'
        }
        Invoke-SetupProcess $python @('-I', '-c', $verify, $venv) $root
    }
    Initialize-SetupCache $cache
    $lockPath = Join-Path $cache 'setup.lock'
    Assert-PlainPath $lockPath
    $lock = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None')
    try {
        $uv = Get-UvExecutable $cache
        Assert-InstallTree $venv
        Assert-InstallTree $cache
        Write-Host 'Preparing the project environment (Internet access may be needed)...'
        Invoke-SetupProcess $uv @('sync', '--project', $root, '--frozen', '--inexact',
            '--python', 'cpython-3.14+gil-windows-x86_64-none') $root
        $verifyInstall = $verify + '; import desktop_mcp; assert pathlib.Path(desktop_mcp.__file__).resolve().is_relative_to(pathlib.Path(sys.argv[2]).resolve()), "Desktop-MCP is not installed from this folder"'
        Invoke-SetupProcess $python @('-I', '-c', $verifyInstall, $venv, $root) $root
        $configArguments = @('-I', (Join-Path $root 'scripts\configure_copilot.py'),
            '--config', $CopilotConfig, '--python', $python)
        if (-not $SkipCopilot) {
            Invoke-SetupProcess $python ($configArguments + @('--check')) $root
        }
        if (-not $SkipShortcut) {
            Invoke-SetupProcess $python @('-I', '-m', 'desktop_mcp', 'install-shortcut') $root
        }
        if (-not $SkipCopilot) { Invoke-SetupProcess $python $configArguments $root }
    } finally { $lock.Dispose() }
    Write-Host 'Setup complete. No application was started or armed.'
    if (-not $SkipShortcut) { Write-Host 'Open Windows Start and search for Desktop-MCP.' }
    Write-Host "Or run in PowerShell: & `"$python`" -m desktop_mcp open"
    if (-not $SkipCopilot) {
        Write-Host 'Use a new Copilot session or reconnect desktop-mcp in /mcp. Copilot, models and sign-in remain separate.'
    }
    Write-Host 'Desktop access starts stopped; only you can choose Arm / Resume locally.'
}

if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference = 'Stop'
    try {
        Invoke-DesktopSetup -WhatIf:$WhatIf -SkipCopilot:$SkipCopilot -SkipShortcut:$SkipShortcut -CopilotConfig $CopilotConfig
        exit 0
    } catch {
        [Console]::Error.WriteLine("Desktop-MCP setup failed: $($_.Exception.Message)")
        [Console]::Error.WriteLine('Setup stopped. Resolve any reported configuration recovery state before retrying; integrations can be skipped explicitly.')
        if ($_.Exception.Data.Contains('ExitCode')) { exit [int]$_.Exception.Data['ExitCode'] }
        exit 1
    }
}
