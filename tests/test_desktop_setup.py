"""Installer tests: owned project fixtures, no downloads, installs, or real desktop effects."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "scripts" / "setup.ps1"
HELPER = ROOT / "scripts" / "configure_copilot.py"
PYTHON = sys.executable
POWERSHELL = Path(os.environ.get("SystemRoot", "")) / (
    r"System32\WindowsPowerShell\v1.0\powershell.exe"
)
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows setup and file semantics")
spec = importlib.util.spec_from_file_location("desktop_setup_config", HELPER)
config_helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config_helper)


@pytest.fixture(scope="module")
def workspace() -> Path:
    # No system temp directory, recursive cleanup, or reuse of somebody else's fixture.
    path = ROOT / "sandbox" / ("setup-" + uuid4().hex[:8])
    path.mkdir(parents=True, exist_ok=False)
    return path


@pytest.fixture
def owned(workspace: Path) -> Path:
    path = workspace / uuid4().hex[:8]
    path.mkdir(exist_ok=False)
    return path


def ps_string(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def run_ps(
    code: str, *, cwd: Path = ROOT, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    script = (
        "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; "
        "[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)\n" + code
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.update(extra_env or {})
    return subprocess.run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def ps_ok(code: str, **kwargs: object) -> str:
    result = run_ps(code, **kwargs)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def load_setup() -> str:
    return f". {ps_string(SETUP)}\n"


def make_install(owned: Path, injection: str = "") -> Path:
    install = owned / "Desktop Ω space's&[x]"
    (install / "scripts").mkdir(parents=True)
    (install / "src" / "desktop_mcp").mkdir(parents=True)
    (install / "Setup.cmd").write_bytes((ROOT / "Setup.cmd").read_bytes())
    script = SETUP.read_text(encoding="utf-8")
    if injection:
        boundary = "\nif ($MyInvocation.InvocationName -ne '.')"
        assert script.count(boundary) == 1
        script = script.replace(boundary, "\n" + injection + boundary)
    (install / "scripts" / "setup.ps1").write_text(script, encoding="utf-8-sig")
    (install / "scripts" / "configure_copilot.py").write_bytes(HELPER.read_bytes())
    (install / "pyproject.toml").write_text('[project]\nname = "desktop-mcp"\n', encoding="utf-8")
    (install / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    for name in ("__main__.py", "launcher.py"):
        (install / "src" / "desktop_mcp" / name).write_text("# fixture\n", encoding="utf-8")
    return install


def run_wrapper(install: Path, cwd: Path, arguments: str) -> subprocess.CompletedProcess:
    # cmd /s /c needs outer command quotes as well as the quoted script filename.
    command = f'"{os.environ["COMSPEC"]}" /d /s /c ""{install / "Setup.cmd"}" {arguments}"'
    return subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, errors="replace", timeout=60
    )


@pytest.fixture
def config_paths(owned: Path) -> tuple[Path, Path]:
    python = owned / "Desktop Ω space" / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fixture, never executed")
    return owned / "copilot" / "mcp-config.json", python


def write_config(path: Path, data: object, *, bom: bool = False) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    if bom:
        content = b"\xef\xbb\xbf" + content
    path.write_bytes(content)
    return content


def expected_entry(python: Path) -> dict:
    return {
        "type": "local",
        "command": str(python),
        "args": ["-m", "desktop_mcp", "serve"],
        "timeout": 45000,
        "tools": ["*"],
    }


def native_security(*, public_children: bool = False, protected: bool = True) -> object:
    """Independent test fixture descriptor; never changes an existing file's ACL."""
    import pywintypes
    import win32api
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    try:
        sid = win32security.ConvertSidToStringSid(
            win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        )
    finally:
        token.Close()
    control = "P" if protected else ""
    flags = "OICI" if public_children else ""
    sddl = f"O:{sid}D:{control}(A;{flags};FA;;;{sid})"
    if public_children:
        sddl += "(A;OICI;GR;;;WD)"
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = (
        win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            sddl, win32security.SDDL_REVISION_1
        )
    )
    attributes.bInheritHandle = False
    return attributes


def native_fixture_file(path: Path, data: bytes, security: object) -> None:
    import win32con
    import win32file

    handle = win32file.CreateFile(
        str(path),
        win32con.GENERIC_WRITE,
        0,
        security,
        win32con.CREATE_NEW,
        win32con.FILE_ATTRIBUTE_NORMAL,
        None,
    )
    try:
        win32file.WriteFile(handle, data)
        win32file.FlushFileBuffers(handle)
    finally:
        handle.Close()


def file_security(path: Path) -> object:
    import win32security

    return win32security.GetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.OWNER_SECURITY_INFORMATION,
    )


@pytest.mark.parametrize("timeout", [1, 45000, 120000, 2**31 - 1])
def test_timeout_is_a_floor_not_a_replacement(config_paths: tuple, timeout: int) -> None:
    config, python = config_paths
    entry = {
        **expected_entry(python),
        "timeout": timeout,
        "tools": [],
        "disabled": True,
        "env": {"TOKEN": "fixture-only"},
    }
    before = write_config(config, {"other": True, "mcpServers": {"desktop-mcp": entry}}, bom=True)
    changed = config_helper.configure(config, python)
    assert changed == (timeout < 45000)
    assert json.loads(config.read_bytes()) == {
        "other": True,
        "mcpServers": {"desktop-mcp": {**entry, "timeout": max(45000, timeout)}},
    }
    if timeout >= 45000:
        assert config.read_bytes() == before


@pytest.mark.parametrize("timeout", [None, False, True, 0, -1, 45000.0, "120000", [], 2**31])
def test_invalid_timeout_is_not_silently_replaced(config_paths: tuple, timeout: object) -> None:
    config, python = config_paths
    before = write_config(
        config, {"mcpServers": {"desktop-mcp": {**expected_entry(python), "timeout": timeout}}}
    )
    with pytest.raises(config_helper.ConfigError, match="timeout must be"):
        config_helper.configure(config, python)
    assert config.read_bytes() == before
    assert list(config.parent.iterdir()) == [config]


def test_every_sensitive_file_is_protected_at_creation_before_payload(
    config_paths: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    import win32con
    import win32file
    import win32security

    config, python = config_paths
    # The parent deliberately permits inherited public reads; the source does not.
    win32file.CreateDirectory(str(config.parent), native_security(public_children=True))
    original = b'{"other":{"env":{"TOKEN":"fixture-secret-only"}}}'
    native_fixture_file(config, original, native_security())
    source_security = file_security(config)
    expected_acl = config_helper.acl_entries(source_security)
    assert len(config_helper.acl_entries(file_security(config.parent))) > len(expected_acl)
    create, write, flush = win32file.CreateFile, win32file.WriteFile, win32file.FlushFileBuffers
    handles = {}
    events = []

    def tracked_create(
        path: str,
        access: int,
        sharing: int,
        security: object,
        disposition: int,
        flags: int,
        template: object,
    ) -> object:
        assert sharing == 0
        assert disposition == win32con.CREATE_NEW
        assert not security.bInheritHandle
        assert (
            security.SECURITY_DESCRIPTOR.GetSecurityDescriptorControl()[0]
            & win32security.SE_DACL_PROTECTED
        )
        handle = create(path, access, sharing, security, disposition, flags, template)
        actual = win32security.GetSecurityInfo(
            handle, win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
        )
        assert actual.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED
        assert config_helper.acl_entries(actual) == expected_acl
        assert win32file.GetFileSize(handle) == 0
        handles[int(handle)] = Path(path)
        events.append(("secured-at-birth", Path(path)))
        return handle

    def tracked_write(handle: object, data: bytes) -> tuple:
        path = handles[int(handle)]
        assert ("secured-at-birth", path) in events
        events.append(("payload", path))
        return write(handle, data)

    def tracked_flush(handle: object) -> None:
        flush(handle)
        events.append(("durable", handles[int(handle)]))

    replace = config_helper.replace_config

    def checked_commit(staged: Path, target: Path, existed: bool) -> None:
        record = staged.with_suffix(".recovery")
        backup = staged.with_suffix(".original")
        assert ("durable", record) in events and ("durable", backup) in events
        assert backup.read_bytes() == original
        events.append(("commit", target))
        replace(staged, target, existed)

    monkeypatch.setattr(win32file, "CreateFile", tracked_create)
    monkeypatch.setattr(win32file, "WriteFile", tracked_write)
    monkeypatch.setattr(win32file, "FlushFileBuffers", tracked_flush)
    monkeypatch.setattr(config_helper, "replace_config", checked_commit)
    assert config_helper.configure(config, python)
    assert {path.suffix for kind, path in events if kind == "payload"} == {
        ".pending",
        ".original",
        ".recovery",
        ".completion-pending",
    }
    assert config_helper.acl_entries(file_security(config)) == expected_acl
    assert (
        file_security(config).GetSecurityDescriptorOwner()
        == source_security.GetSecurityDescriptorOwner()
    )


@pytest.mark.parametrize("protected", [False, True])
def test_final_existing_acl_is_preserved(config_paths: tuple, protected: bool) -> None:
    import win32file
    import win32security

    config, python = config_paths
    win32file.CreateDirectory(str(config.parent), native_security(public_children=True))
    native_fixture_file(config, b'{"keep":true}', native_security(protected=protected))
    original = file_security(config)
    assert config_helper.configure(config, python)
    final = file_security(config)
    assert config_helper.acl_entries(final) == config_helper.acl_entries(original)
    assert final.GetSecurityDescriptorOwner() == original.GetSecurityDescriptorOwner()
    assert (final.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED) == (
        original.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED
    )


def test_new_config_does_not_inherit_public_read_permissions(config_paths: tuple) -> None:
    import win32file
    import win32security

    config, python = config_paths
    win32file.CreateDirectory(str(config.parent), native_security(public_children=True))
    assert config_helper.configure(config, python)
    final = file_security(config)
    assert final.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED
    assert config_helper.acl_entries(final) == config_helper.acl_entries(
        native_security().SECURITY_DESCRIPTOR
    )


@pytest.mark.parametrize("failure", ["create", "unprotected", "wrong-acl"])
def test_security_failure_writes_no_payload(
    owned: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import win32file
    import win32security

    target = owned / "sensitive.pending"
    expected = native_security()
    writes = []
    monkeypatch.setattr(win32file, "WriteFile", lambda *args: writes.append(args))
    if failure == "create":

        def cannot_create(*args: object) -> None:
            raise OSError("simulated security-establishment failure")

        monkeypatch.setattr(win32file, "CreateFile", cannot_create)
        error = OSError
    else:
        wrong = native_security(
            public_children=failure == "wrong-acl", protected=failure != "unprotected"
        )
        monkeypatch.setattr(
            win32security, "GetSecurityInfo", lambda *args: wrong.SECURITY_DESCRIPTOR
        )
        error = config_helper.ConfigError
    with pytest.raises(error):
        config_helper.write_protected(target, b"fixture-secret-only", expected)
    assert not writes
    assert not target.exists() or target.stat().st_size == 0


@pytest.mark.parametrize("code", [1176, 1177])
@pytest.mark.parametrize("pywin32_error", [False, True])
def test_partial_native_outcomes_keep_recovery_copy_and_block_rerun(
    config_paths: tuple, monkeypatch: pytest.MonkeyPatch, code: int, pywin32_error: bool
) -> None:
    import ctypes
    import pywintypes

    config, python = config_paths
    original = write_config(config, {"other": {"env": {"TOKEN": "fixture-only"}}})
    model = {"target": original}

    def partial_commit(staged: Path, target: Path, existed: bool) -> None:
        assert existed
        record = staged.with_suffix(".recovery")
        metadata = json.loads(record.read_bytes())
        assert metadata["original"] == staged.with_suffix(".original").name
        assert staged.with_suffix(".original").read_bytes() == original
        model["replacement-name"] = staged.read_bytes()
        # Model the documented namespace changes only in memory. Never delete or
        # rename a real original to provoke a native partial-completion failure.
        model["target"] = None
        if code == 1177:
            model["renamed-original"] = original
        if pywin32_error:
            raise pywintypes.error(code, "ReplaceFileW", "simulated native failure")
        raise ctypes.WinError(code)

    monkeypatch.setattr(config_helper, "replace_config", partial_commit)
    with pytest.raises(config_helper.RecoveryRequired, match=f"failed \\({code}\\)"):
        config_helper.configure(config, python)
    assert model["target"] is None
    if code == 1177:
        assert model["renamed-original"] == original
    assert config.read_bytes() == original  # The real fixture was never removed.
    backups = list(config.parent.glob("*.original"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert not list(config.parent.glob("*.complete"))

    def must_not_assume_new(path: Path) -> None:
        pytest.fail(
            "Rerun attempted to interpret the uncertain target instead of its recovery record"
        )

    monkeypatch.setattr(config_helper, "read_state", must_not_assume_new)
    with pytest.raises(config_helper.RecoveryRequired):
        config_helper.configure(config, python)
    with pytest.raises(config_helper.RecoveryRequired):
        config_helper.configure(config, python, check=True)


@pytest.mark.parametrize("concurrent", [False, True])
def test_preconstructed_recovery_state_never_creates_or_overwrites_config(
    config_paths: tuple, concurrent: bool
) -> None:
    config, python = config_paths
    config.parent.mkdir()
    original = b'{"old":"fixture-only"}'
    backup = config.with_name(config.name + ".desktop-mcp-fixture.original")
    record = backup.with_suffix(".recovery")
    native_fixture_file(backup, original, native_security())
    native_fixture_file(record, b'{"version":1}', native_security())
    current = None
    if concurrent:
        current = write_config(config, {"concurrent": {"command": "keep.exe"}})
    with pytest.raises(config_helper.RecoveryRequired):
        config_helper.configure(config, python)
    assert (config.read_bytes() if config.exists() else None) == current
    assert backup.read_bytes() == original


@pytest.mark.parametrize("check", [False, True])
@pytest.mark.parametrize("config_name", ["MCP-CONFIG.JSON", "mCp-CoNfIg.JsOn"])
def test_unfinished_recovery_uses_windows_filename_identity(
    config_paths: tuple, check: bool, config_name: str
) -> None:
    config, python = config_paths
    config.parent.mkdir()
    backup = config.parent / "Mcp-Config.Json.DeSkToP-McP-fixture.ORIGINAL"
    record = backup.with_suffix(".ReCoVeRy")
    original = b'{"other":{"env":{"TOKEN":"fixture-only"}}}'
    native_fixture_file(backup, original, native_security())
    native_fixture_file(record, b'{"version":1}', native_security())
    alternate = config.with_name(config_name)
    with pytest.raises(config_helper.RecoveryRequired):
        config_helper.configure(alternate, python, check=check)
    assert not alternate.exists()
    assert backup.read_bytes() == original
    assert len(list(config.parent.iterdir())) == 2


@pytest.mark.parametrize("check", [False, True])
def test_recovery_cli_check_also_blocks_alternate_config_case(
    config_paths: tuple, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, check: bool
) -> None:
    config, python = config_paths
    config.parent.mkdir()
    record = config.parent / "mcp-config.json.DESKTOP-MCP-fixture.RECOVERY"
    native_fixture_file(record, b'{"version":1}', native_security())
    alternate = config.with_name("MCP-CONFIG.JSON")
    arguments = ["configure_copilot", "--config", str(alternate), "--python", str(python)]
    if check:
        arguments.append("--check")
    monkeypatch.setattr(sys, "argv", arguments)
    assert config_helper.main() == 1
    output = capsys.readouterr()
    assert "recovery required" in output.err
    assert "Traceback" not in output.err
    assert not alternate.exists()


@pytest.mark.parametrize("api", ["GetSecurityInfo", "CreateFile", "WriteFile", "FlushFileBuffers"])
def test_real_pywin32_error_types_are_sanitized_without_permission_experiments(
    config_paths: tuple, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, api: str
) -> None:
    import pywintypes
    import win32file
    import win32security

    config, python = config_paths
    before = write_config(config, {"other": {"env": {"TOKEN": "fixture-only"}}})
    failure = pywintypes.error(5, api, "sensitive-native-fixture-text")
    assert not isinstance(failure, OSError)

    def fail(*args: object) -> None:
        raise failure

    module = win32security if api == "GetSecurityInfo" else win32file
    monkeypatch.setattr(module, api, fail)
    monkeypatch.setattr(
        sys, "argv", ["configure_copilot", "--config", str(config), "--python", str(python)]
    )
    assert config_helper.main() == 1
    output = capsys.readouterr()
    assert "did not finish" in output.err
    assert "sensitive-native-fixture-text" not in output.out + output.err
    assert "Traceback" not in output.err
    assert config.read_bytes() == before
    assert not list(config.parent.glob("*.recovery"))


def test_pywin32_completion_flush_failure_is_not_published_as_success(
    config_paths: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pywintypes
    import win32file

    config, python = config_paths
    before = write_config(config, {"keep": "fixture-only"})
    create, flush = win32file.CreateFile, win32file.FlushFileBuffers
    paths = {}

    def tracked_create(path: str, *arguments: object) -> object:
        handle = create(path, *arguments)
        paths[int(handle)] = Path(path)
        return handle

    def failed_flush(handle: object) -> None:
        if paths[int(handle)].suffix == ".completion-pending":
            raise pywintypes.error(5, "FlushFileBuffers", "simulated native flush failure")
        flush(handle)

    monkeypatch.setattr(win32file, "CreateFile", tracked_create)
    monkeypatch.setattr(win32file, "FlushFileBuffers", failed_flush)
    with pytest.raises(config_helper.RecoveryRequired) as failure:
        config_helper.configure(config, python)
    assert isinstance(failure.value.__cause__, pywintypes.error)
    assert failure.value.__cause__.winerror == 5
    assert json.loads(config.read_bytes())["mcpServers"]["desktop-mcp"] == expected_entry(python)
    assert next(config.parent.glob("*.original")).read_bytes() == before
    assert next(config.parent.glob("*.completion-pending")).read_bytes() == b"committed\n"
    assert not list(config.parent.glob("*.complete"))
    with pytest.raises(config_helper.RecoveryRequired):
        config_helper.configure(config, python)
    with pytest.raises(config_helper.RecoveryRequired):
        config_helper.configure(config, python, check=True)


def test_completion_record_failure_is_recovery_required_not_success(
    config_paths: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, python = config_paths
    original = write_config(config, {"keep": "fixture-only"})
    write = config_helper.write_protected

    def fail_completion(path: Path, payload: bytes, security: object) -> None:
        if path.suffix == ".completion-pending":
            raise OSError("simulated completion-marker failure")
        write(path, payload, security)

    monkeypatch.setattr(config_helper, "write_protected", fail_completion)
    with pytest.raises(config_helper.RecoveryRequired):
        config_helper.configure(config, python)
    assert json.loads(config.read_bytes())["mcpServers"]["desktop-mcp"] == expected_entry(python)
    assert next(config.parent.glob("*.original")).read_bytes() == original
    with pytest.raises(config_helper.RecoveryRequired):
        config_helper.configure(config, python)


def test_new_configuration_is_absolute_supervised_and_idempotent(config_paths: tuple) -> None:
    config, python = config_paths
    assert config_helper.configure(config, python)
    assert json.loads(config.read_bytes()) == {
        "mcpServers": {"desktop-mcp": expected_entry(python)}
    }
    before = config.read_bytes(), config.stat().st_mtime_ns
    assert not config_helper.configure(config, python)
    assert (config.read_bytes(), config.stat().st_mtime_ns) == before
    assert not list(config.parent.glob("*.pending"))


@pytest.mark.parametrize("tools", [[], ["DesktopStatus"], ["*"]])
def test_merge_preserves_bom_other_servers_env_restrictions_and_top_level(
    config_paths: tuple, tools: list[str]
) -> None:
    config, python = config_paths
    entry = expected_entry(python)
    entry.update(
        timeout=1000,
        tools=tools,
        env={"TOKEN": "fixture secret Ω", "CUSTOM": "value"},
        disabled=True,
    )
    other = {"type": "local", "command": "other.exe", "env": {"TOKEN": "another fixture secret"}}
    original = {
        "top": {"nested": [1, True, None, "Ω"]},
        "mcpServers": {"other": other, "desktop-mcp": entry},
    }
    write_config(config, original, bom=True)
    assert config_helper.configure(config, python)
    result = json.loads(config.read_bytes())
    assert result["top"] == original["top"]
    assert result["mcpServers"]["other"] == other
    assert result["mcpServers"]["desktop-mcp"] == {**entry, "timeout": 45000}
    assert "Ω".encode("utf-8") in config.read_bytes()


def test_already_correct_bom_json_is_not_reformatted(config_paths: tuple) -> None:
    config, python = config_paths
    before = write_config(config, {"mcpServers": {"desktop-mcp": expected_entry(python)}}, bom=True)
    assert not config_helper.configure(config, python)
    assert config.read_bytes() == before
    assert list(config.parent.iterdir()) == [config]


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\xff",
        b'{"TOKEN":"fixture secret",',
        b"[]",
        b'{"mcpServers":null}',
        b'{"mcpServers":[]}',
        b'{"mcpServers":{},"mcpServers":{}}',
        b'{"unknown":NaN}',
        b'{"mcpServers":{"desktop-mcp":null}}',
    ],
)
def test_invalid_configuration_is_never_reset(config_paths: tuple, content: bytes) -> None:
    config, python = config_paths
    config.parent.mkdir()
    config.write_bytes(content)
    with pytest.raises(config_helper.ConfigError):
        config_helper.configure(config, python)
    assert config.read_bytes() == content
    assert list(config.parent.iterdir()) == [config]


@pytest.mark.parametrize(
    "change",
    [
        {"command": "python.exe"},
        {"command": r"C:\another application\python.exe"},
        {"args": ["-m", "windows_mcp"]},
        {"args": ["-m", "desktop_mcp", "open"]},
        {"type": "http", "url": "https://example.invalid"},
        {"tools": None},
    ],
)
def test_conflicting_registration_or_invalid_restrictions_are_preserved(
    config_paths: tuple, change: dict
) -> None:
    config, python = config_paths
    before = write_config(
        config, {"mcpServers": {"desktop-mcp": {**expected_entry(python), **change}}}
    )
    with pytest.raises(config_helper.ConfigError):
        config_helper.configure(config, python)
    assert config.read_bytes() == before


def test_config_check_does_not_create_directories(config_paths: tuple) -> None:
    config, python = config_paths
    assert config_helper.configure(config, python, check=True)
    assert not config.parent.exists()


def test_merge_cannot_write_a_config_too_large_for_its_next_run(config_paths: tuple) -> None:
    config, python = config_paths
    config.parent.mkdir()
    before = json.dumps({"extra": [0] * 700000}, separators=(",", ":")).encode("utf-8")
    assert len(before) < 4 * 1024 * 1024
    config.write_bytes(before)
    with pytest.raises(config_helper.ConfigError, match="4 MiB"):
        config_helper.configure(config, python)
    assert config.read_bytes() == before
    assert not list(config.parent.glob("*.pending"))


@pytest.mark.parametrize("exists", [False, True])
def test_concurrent_config_change_is_detected_before_replacement(
    config_paths: tuple, monkeypatch: pytest.MonkeyPatch, exists: bool
) -> None:
    config, python = config_paths
    if exists:
        write_config(config, {"mcpServers": {"other": {"command": "keep.exe"}}})
    read = config_helper.read_state
    calls = 0
    concurrent = {"mcpServers": {"concurrent": {"command": "new.exe"}}}

    def changing_read(path: Path) -> tuple:
        nonlocal calls
        calls += 1
        if calls == 3:
            write_config(path, concurrent)
        return read(path)

    monkeypatch.setattr(config_helper, "read_state", changing_read)
    with pytest.raises(config_helper.ConfigError, match="changed before replacement"):
        config_helper.configure(config, python)
    assert json.loads(config.read_bytes()) == concurrent
    pending = list(config.parent.glob("*.pending"))
    assert len(pending) == 1
    assert json.loads(pending[0].read_bytes())["mcpServers"]["desktop-mcp"] == expected_entry(
        python
    )


def test_late_creation_cannot_be_overwritten(
    config_paths: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, python = config_paths
    replace = config_helper.replace_config
    concurrent = {"mcpServers": {"new": {"command": "keep.exe"}}}

    def racing_replace(staged: Path, target: Path, existed: bool) -> None:
        write_config(target, concurrent)
        replace(staged, target, existed)

    monkeypatch.setattr(config_helper, "replace_config", racing_replace)
    with pytest.raises(config_helper.RecoveryRequired, match="recovery required"):
        config_helper.configure(config, python)
    assert json.loads(config.read_bytes()) == concurrent


def test_atomic_write_failure_leaves_original_and_complete_pending_json(
    config_paths: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, python = config_paths
    before = write_config(config, {"top": {"TOKEN": "fixture secret"}})

    def fail_replace(*args: object) -> None:
        raise OSError("simulated sharing violation")

    monkeypatch.setattr(config_helper, "replace_config", fail_replace)
    with pytest.raises(config_helper.RecoveryRequired, match="recovery required"):
        config_helper.configure(config, python)
    assert config.read_bytes() == before
    pending = list(config.parent.glob("*.pending"))
    assert len(pending) == 1
    assert json.loads(pending[0].read_bytes())["top"] == json.loads(before)["top"]


def test_setup_config_lock_conflict_is_explicit(
    config_paths: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    import msvcrt

    config, python = config_paths
    before = write_config(config, {})

    def locked(*args: object) -> None:
        raise OSError("simulated existing lock")

    monkeypatch.setattr(msvcrt, "locking", locked)
    with pytest.raises(config_helper.ConfigError, match="Another setup"):
        config_helper.configure(config, python)
    assert config.read_bytes() == before
    assert not list(config.parent.glob("*.pending"))


def test_hard_linked_config_is_rejected(config_paths: tuple) -> None:
    config, python = config_paths
    original = python.parent / "linked.json"
    original.write_text("{}", encoding="utf-8")
    config.parent.mkdir()
    os.link(original, config)
    with pytest.raises(config_helper.ConfigError, match="Hard-linked"):
        config_helper.configure(config, python)
    assert original.read_text(encoding="utf-8") == "{}"


def test_config_cli_errors_never_print_contents(config_paths: tuple) -> None:
    config, python = config_paths
    config.parent.mkdir()
    config.write_text('{"TOKEN":"sensitive-fixture-value",', encoding="utf-8")
    result = subprocess.run(
        [PYTHON, "-I", "-B", str(HELPER), "--config", str(config), "--python", str(python)],
        cwd=python.parent,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert "not valid UTF-8 JSON" in result.stderr
    assert "sensitive-fixture-value" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_wrapper_whatif_works_from_arbitrary_cwd_without_python_or_uv(owned: Path) -> None:
    install = make_install(owned)
    elsewhere = owned / "unrelated cwd"
    elsewhere.mkdir()
    before = sorted(str(path.relative_to(owned)) for path in owned.rglob("*"))
    result = run_ps(
        f"""
$env:PATH = {ps_string(elsewhere)}
& {ps_string(install / "scripts" / "setup.ps1")} -WhatIf -CopilotConfig {ps_string(owned / "unused.json")}
""",
        cwd=elsewhere,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert str(install) in result.stdout
    assert "PLAN ONLY" in result.stdout
    assert sorted(str(path.relative_to(owned)) for path in owned.rglob("*")) == before
    result = run_wrapper(install, elsewhere, "-WhatIf")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PLAN ONLY" in result.stdout
    assert not (install / ".desktop-mcp-setup-cache").exists()


def test_invalid_root_and_native_architecture_fail_before_setup(owned: Path) -> None:
    missing = owned / "not a project"
    missing.mkdir()
    result = run_ps(
        load_setup() + f"$script:SetupRoot = {ps_string(missing)}; Invoke-DesktopSetup -WhatIf"
    )
    assert result.returncode != 0
    assert "Incomplete Desktop-MCP" in result.stderr
    result = run_ps(
        load_setup()
        + "$env:PROCESSOR_ARCHITEW6432 = 'ARM64'; $env:PROCESSOR_ARCHITECTURE = 'AMD64'; Invoke-DesktopSetup -WhatIf"
    )
    assert result.returncode != 0
    assert "ARM64/32-bit" in result.stderr


def test_noop_plan_skips_every_side_effect_and_integration(owned: Path) -> None:
    output = ps_ok(
        load_setup()
        + """
function Initialize-SetupCache { throw 'unexpected cache write' }
function Get-UvExecutable { throw 'unexpected bootstrap' }
function Invoke-SetupProcess { throw 'unexpected process' }
Invoke-DesktopSetup -WhatIf -SkipShortcut -SkipCopilot
""",
        cwd=owned,
    )
    assert "No downloads, processes, configuration reads, or writes" in output
    assert "merge desktop-mcp" not in output
    assert "install-shortcut command" not in output


def test_process_environment_and_windows_argv_quoting(owned: Path) -> None:
    arguments = [
        "",
        "space Ω",
        'a"quote',
        "one\\",
        'slashes\\\\"quote',
        "x&y;$(not code)",
        str(owned),
    ]
    args_literal = ", ".join(ps_string(arg) for arg in arguments)
    probe = "import json, os, sys; print(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd(), 'env': {k: v for k, v in os.environ.items() if k.startswith('UV_') or k in ('VIRTUAL_ENV', 'CONDA_PREFIX', 'PYTHONPATH', 'PYTHONHOME', 'TEMP', 'TMP')}}))"
    output = ps_ok(
        load_setup()
        + f"Invoke-SetupProcess {ps_string(PYTHON)} @('-I', '-B', '-c', {ps_string(probe)}, {args_literal}) {ps_string(owned)}",
        extra_env={
            "UV_PROJECT_ENVIRONMENT": str(owned / "foreign-environment"),
            "UV_PROJECT": str(owned / "foreign-project"),
            "UV_CONFIG_FILE": str(owned / "foreign-uv.toml"),
            "UV_NO_INSTALL_PROJECT": "1",
            "UV_PYTHON_INSTALL_DIR": str(owned / "foreign-python"),
            "UV_PYTHON_INSTALL_REGISTRY": "1",
            "VIRTUAL_ENV": str(owned / "foreign-active"),
            "CONDA_PREFIX": str(owned / "foreign-conda"),
            "PYTHONHOME": str(owned / "foreign-home"),
            "PYTHONPATH": str(owned / "foreign-imports"),
        },
    )
    result = json.loads(output)
    assert result["args"] == arguments
    assert Path(result["cwd"]) == owned
    env = result["env"]
    assert env["UV_PROJECT"] == str(owned)
    assert env["UV_PROJECT_ENVIRONMENT"] == str(owned / ".venv")
    assert env["UV_PYTHON_INSTALL_DIR"] == str(owned / ".desktop-mcp-setup-cache" / "python")
    assert env["UV_PYTHON_INSTALL_REGISTRY"] == env["UV_PYTHON_INSTALL_BIN"] == "0"
    assert env["UV_PYTHON_NO_REGISTRY"] == "1"
    assert env["TEMP"] == env["TMP"] == str(owned / ".desktop-mcp-setup-cache" / "work")
    assert (
        not {
            "PYTHONPATH",
            "PYTHONHOME",
            "VIRTUAL_ENV",
            "CONDA_PREFIX",
            "UV_CONFIG_FILE",
            "UV_NO_INSTALL_PROJECT",
        }
        & env.keys()
    )


def test_child_failure_retains_exit_code(owned: Path) -> None:
    result = run_ps(
        load_setup()
        + f"""
try {{ Invoke-SetupProcess {ps_string(PYTHON)} @('-I', '-B', '-c', 'import sys; sys.exit(37)') {ps_string(owned)} }}
catch {{ if ($_.Exception.Data['ExitCode'] -eq 37) {{ exit 37 }}; throw }}
"""
    )
    assert result.returncode == 37


@pytest.mark.parametrize("skip", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_setup_orders_existing_cli_and_config_preflight_and_preserves_current_directory(
    owned: Path, skip: bool, existing: bool
) -> None:
    trace = owned / "calls.jsonl"
    injection = f"""
function Get-UvExecutable([string]$Cache) {{ return (Join-Path $Cache 'fixture-uv.exe') }}
function Invoke-SetupProcess([string]$Executable, [string[]]$Arguments, [string]$Root) {{
    $info = New-SetupProcess $Executable $Arguments $Root
    $record = @{{ executable = $Executable; arguments = $Arguments; cwd = $info.WorkingDirectory;
        environment = $info.EnvironmentVariables['UV_PROJECT_ENVIRONMENT'] }}
    [IO.File]::AppendAllText({ps_string(trace)}, (($record | ConvertTo-Json -Compress) + "`n"))
}}
"""
    install = make_install(owned, injection)
    if existing:
        scripts = install / ".venv" / "Scripts"
        scripts.mkdir(parents=True)
        (scripts / "python.exe").write_bytes(b"mock interpreter; never executed")
        (scripts.parent / "pyvenv.cfg").write_text("fixture", encoding="utf-8")
        (scripts.parent / "unrelated-dev-extra").write_text("keep", encoding="utf-8")
    config = owned / "untouched.json"
    flags = "-SkipCopilot -SkipShortcut" if skip else ""
    output = ps_ok(
        f"& {ps_string(install / 'scripts' / 'setup.ps1')} -CopilotConfig {ps_string(config)} {flags}",
        cwd=owned,
    )
    calls = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    count = len(calls)
    if existing:
        assert calls[0]["arguments"][:2] == ["-I", "-c"]
        calls = calls[1:]
    assert calls[0]["arguments"] == [
        "sync",
        "--project",
        str(install),
        "--frozen",
        "--inexact",
        "--python",
        "cpython-3.14+gil-windows-x86_64-none",
    ]
    assert all(call["cwd"] == str(install) for call in calls)
    assert all(call["environment"] == str(install / ".venv") for call in calls)
    assert all(
        call["executable"] == str(install / ".venv" / "Scripts" / "python.exe")
        for call in calls[1:]
    )
    assert len(calls) == (2 if skip else 5)
    assert "; import desktop_mcp;" in calls[1]["arguments"][2]
    assert calls[1]["arguments"][-2:] == [str(install / ".venv"), str(install)]
    if not skip:
        assert calls[2]["arguments"][-1] == "--check"
        assert calls[3]["arguments"] == ["-I", "-m", "desktop_mcp", "install-shortcut"]
        assert "--check" not in calls[4]["arguments"]
        assert str(config) in calls[4]["arguments"]
    assert "No application was started or armed" in output
    assert not config.exists()
    ps_ok(
        f"& {ps_string(install / 'scripts' / 'setup.ps1')} -CopilotConfig {ps_string(config)} {flags}",
        cwd=owned,
    )
    assert len(trace.read_text(encoding="utf-8").splitlines()) == count * 2
    if existing:
        assert (install / ".venv" / "unrelated-dev-extra").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("failure_stage", ["sync", "--check", "install-shortcut"])
def test_integration_failure_stops_later_steps_and_wrapper_preserves_code(
    owned: Path, failure_stage: str
) -> None:
    trace = owned / "calls.jsonl"
    injection = f"""
function Get-UvExecutable([string]$Cache) {{ return (Join-Path $Cache 'fixture-uv.exe') }}
function Invoke-SetupProcess([string]$Executable, [string[]]$Arguments, [string]$Root) {{
    [IO.File]::AppendAllText({ps_string(trace)}, (($Arguments | ConvertTo-Json -Compress) + "`n"))
    if ($Arguments -contains {ps_string(failure_stage)}) {{
        $failure = New-Object InvalidOperationException('simulated stage failure')
        $failure.Data['ExitCode'] = 37
        throw $failure
    }}
}}
"""
    install = make_install(owned, injection)
    result = run_wrapper(install, owned, f'-CopilotConfig "{owned / "never-written.json"}"')
    assert result.returncode == 37, result.stdout + result.stderr
    calls = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == {"sync": 1, "--check": 3, "install-shortcut": 4}[failure_stage]
    assert "Setup complete" not in result.stdout
    assert not (owned / "never-written.json").exists()


def test_existing_incomplete_environment_is_never_recreated(owned: Path) -> None:
    install = make_install(owned)
    venv = install / ".venv"
    venv.mkdir()
    sentinel = venv / "keep.txt"
    sentinel.write_text("unrelated existing data", encoding="utf-8")
    result = run_ps(f"& {ps_string(install / 'scripts' / 'setup.ps1')} -SkipCopilot -SkipShortcut")
    assert result.returncode != 0
    assert "incomplete/different .venv" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "unrelated existing data"
    assert not (install / ".desktop-mcp-setup-cache").exists()


def test_ordinary_rerun_accepts_only_uvs_owned_minor_junction_without_traversing_it(
    owned: Path,
) -> None:
    import _winapi

    trace = owned / "alias-trace.txt"
    injection = f"""
$script:RealChildPaths = (Get-Command Get-SetupChildPaths).ScriptBlock
function Get-SetupChildPaths([string]$Path) {{
    $pythonRoot = Join-Path $script:SetupRoot '.desktop-mcp-setup-cache\\python'
    if ($Path -eq (Join-Path $pythonRoot 'cpython-3.14-windows-x86_64-none')) {{
        throw 'TEST FAILURE: traversed the junction alias'
    }}
    if ($Path -eq (Join-Path $pythonRoot 'cpython-3.14.7-windows-x86_64-none')) {{
        [IO.File]::AppendAllText({ps_string(trace)}, "walk-patch`n")
    }}
    return ,(& $script:RealChildPaths $Path)
}}
function Get-UvExecutable([string]$Cache) {{ return (Join-Path $Cache 'fixture-uv.exe') }}
function Invoke-SetupProcess([string]$Executable, [string[]]$Arguments, [string]$Root) {{
    if ($Arguments[0] -eq 'sync') {{
        [IO.File]::AppendAllText({ps_string(trace)}, "sync`n")
        $scripts = Join-Path $Root '.venv\\Scripts'
        [void][IO.Directory]::CreateDirectory($scripts)
        [IO.File]::WriteAllText((Join-Path $scripts 'python.exe'), 'fixture, never executed')
        [IO.File]::WriteAllText((Join-Path $Root '.venv\\pyvenv.cfg'), 'fixture')
        $pythonRoot = Join-Path $Root '.desktop-mcp-setup-cache\\python'
        $patch = Join-Path $pythonRoot 'cpython-3.14.7-windows-x86_64-none'
        [void][IO.Directory]::CreateDirectory((Join-Path $patch 'Lib'))
        [IO.File]::WriteAllText((Join-Path $patch 'python.exe'), 'fixture, never executed')
    }}
}}
"""
    install = make_install(owned, injection)
    command = (
        f"& {ps_string(install / 'scripts' / 'setup.ps1')} "
        f"-CopilotConfig {ps_string(owned / 'unused.json')}"
    )
    assert "Setup complete" in ps_ok(command, cwd=owned)
    assert trace.read_text(encoding="utf-8").splitlines() == ["sync"]
    python_root = install / ".desktop-mcp-setup-cache" / "python"
    # Reproduce uv's post-install junction with literal paths, not PowerShell's
    # wildcard-sensitive New-Item target resolution.
    _winapi.CreateJunction(
        str(python_root / "cpython-3.14.7-windows-x86_64-none"),
        str(python_root / "cpython-3.14-windows-x86_64-none"),
    )
    assert "Setup complete" in ps_ok(command, cwd=owned)
    events = trace.read_text(encoding="utf-8").splitlines()
    assert events.count("sync") == 2
    assert "walk-patch" in events[1:-1]
    assert not (owned / "unused.json").exists()


@pytest.mark.parametrize(
    "case",
    [
        "wrong-name",
        "wrong-location",
        "venv-location",
        "symlink",
        "file-link",
        "wrong-version",
        "wrong-architecture",
        "wrong-variant",
        "escaping",
        "dotdot",
        "broken",
        "target-file",
        "alias-chain",
        "target-child-link",
        "multiple-targets",
        "missing-owner",
        "wrong-owner",
    ],
)
def test_managed_alias_exception_rejects_foreign_broken_or_chained_links(
    owned: Path, case: str
) -> None:
    install_path = owned / "Desktop Ω space's&[x]"
    cache = install_path / ".desktop-mcp-setup-cache"
    python_root = cache / "python"
    patch = python_root / "cpython-3.14.7-windows-x86_64-none"
    alias = python_root / "cpython-3.14-windows-x86_64-none"
    if case == "wrong-name":
        alias = python_root / "cpython-3.13-windows-x86_64-none"
    if case == "wrong-location":
        alias = cache / "packages" / "cpython-3.14-windows-x86_64-none"
    if case == "venv-location":
        alias = install_path / ".venv" / "Lib" / "cpython-3.14-windows-x86_64-none"
    target = str(patch)
    if case == "wrong-version":
        target = str(python_root / "cpython-3.13.7-windows-x86_64-none")
    elif case == "wrong-architecture":
        target = str(python_root / "cpython-3.14.7-windows-aarch64-none")
    elif case == "wrong-variant":
        target = str(python_root / "cpython-3.14.7+freethreaded-windows-x86_64-none")
    elif case == "escaping":
        target = str(owned / "outside-cache" / patch.name)
    elif case == "dotdot":
        target = str(python_root) + r"\..\..\outside-cache\\" + patch.name
    elif case == "broken":
        target = str(python_root / "cpython-3.14.99-windows-x86_64-none")
    link_type = "SymbolicLink" if case == "symlink" else "Junction"
    attributes = "[IO.FileAttributes]::ReparsePoint"
    if case != "file-link":
        attributes += " -bor [IO.FileAttributes]::Directory"
    targets = ps_string(target)
    if case == "multiple-targets":
        targets += ", " + ps_string(str(python_root / "cpython-3.14.8-windows-x86_64-none"))
    chained = patch if case == "alias-chain" else patch / "Lib" / "linked-child"
    chain_override = ""
    if case in ("alias-chain", "target-child-link"):
        chain_override = f"""
    if ($Path -eq {ps_string(chained)}) {{
        return [pscustomobject]@{{ Attributes = [IO.FileAttributes]::Directory -bor [IO.FileAttributes]::ReparsePoint;
            LinkType = 'Junction'; Target = @({ps_string(str(python_root / "cpython-3.14.8-windows-x86_64-none"))}) }}
    }}
"""
    injection = f"""
$script:RealPathInfo = (Get-Command Get-SetupPathInfo).ScriptBlock
$script:RealChildPaths = (Get-Command Get-SetupChildPaths).ScriptBlock
function Get-SetupPathInfo([string]$Path) {{
    if ($Path -eq {ps_string(alias)}) {{
        return [pscustomobject]@{{ Attributes = {attributes}; LinkType = '{link_type}'; Target = @({targets}) }}
    }}
    if ($Path -eq {ps_string(owned / "outside-cache" / patch.name)}) {{
        throw 'TEST FAILURE: inspected an escaping target'
    }}
    {chain_override}
    return (& $script:RealPathInfo $Path)
}}
function Get-SetupChildPaths([string]$Path) {{
    if ($Path -eq {ps_string(alias)} -or
        ({ps_string(case)} -in @('alias-chain', 'target-child-link') -and $Path -eq {ps_string(chained)})) {{
        throw 'TEST FAILURE: followed a link'
    }}
    return ,(& $script:RealChildPaths $Path)
}}
function Invoke-SetupProcess {{ throw 'TEST FAILURE: ran a process before validation' }}
function Get-UvExecutable {{ throw 'TEST FAILURE: bootstrapped before validation' }}
"""
    install = make_install(owned, injection)
    alias.mkdir(parents=True)
    if case == "target-file":
        patch.write_text("not a directory", encoding="utf-8")
    else:
        (patch / "Lib" / "linked-child").mkdir(parents=True)
    if case != "missing-owner":
        (cache / "owner.txt").write_text(
            "foreign cache" if case == "wrong-owner" else "Desktop-MCP setup cache v1",
            encoding="utf-8",
        )
    result = run_wrapper(install, owned, "-SkipCopilot -SkipShortcut")
    assert result.returncode != 0
    assert "TEST FAILURE" not in result.stdout + result.stderr
    assert "linked/junction" in result.stderr or "ownership marker" in result.stderr
    assert alias.is_dir()  # All negative link metadata was faked, never traversed.


def test_dangling_owned_native_alias_is_rejected_without_following_it(owned: Path) -> None:
    import _winapi

    install = make_install(owned)
    cache = install / ".desktop-mcp-setup-cache"
    python_root = cache / "python"
    python_root.mkdir(parents=True)
    (cache / "owner.txt").write_text("Desktop-MCP setup cache v1", encoding="utf-8")
    alias = python_root / "cpython-3.14-windows-x86_64-none"
    missing_target = python_root / "cpython-3.14.99-windows-x86_64-none"
    missing_target.mkdir()
    _winapi.CreateJunction(str(missing_target), str(alias))
    # Keep the explicitly created, empty fixture directory under another name;
    # no data is deleted to construct the dangling-link state.
    os.rename(missing_target, python_root / "retained-empty-fixture")
    result = run_ps(
        f". {ps_string(install / 'scripts' / 'setup.ps1')}\n"
        + f"""
$script:RealChildPaths = (Get-Command Get-SetupChildPaths).ScriptBlock
function Get-SetupChildPaths([string]$Path) {{
    if ($Path -eq {ps_string(alias)}) {{ throw 'TEST FAILURE: traversed dangling alias' }}
    return ,(& $script:RealChildPaths $Path)
}}
Assert-InstallTree {ps_string(cache)}
"""
    )
    assert result.returncode != 0
    assert "linked/junction" in result.stderr
    assert "TEST FAILURE" not in result.stderr
    assert not missing_target.exists()


@pytest.mark.parametrize("prefix", ["", "\\??\\", "\\\\?\\"])
def test_managed_alias_target_is_visited_separately_under_the_same_guard(
    owned: Path, prefix: str
) -> None:
    install = make_install(owned)
    cache = install / ".desktop-mcp-setup-cache"
    python_root = cache / "python"
    patch = python_root / "cpython-3.14.7-windows-x86_64-none"
    alias = python_root / "cpython-3.14-windows-x86_64-none"
    (patch / "Lib").mkdir(parents=True)
    alias.mkdir()
    (cache / "owner.txt").write_text("Desktop-MCP setup cache v1", encoding="utf-8")
    output = ps_ok(
        f". {ps_string(install / 'scripts' / 'setup.ps1')}\n"
        + f"""
$script:RealPathInfo = (Get-Command Get-SetupPathInfo).ScriptBlock
$script:RealChildPaths = (Get-Command Get-SetupChildPaths).ScriptBlock
$script:VisitedPatch = 0
function Get-SetupPathInfo([string]$Path) {{
    if ($Path -eq {ps_string(alias)}) {{
        return [pscustomobject]@{{ Attributes = [IO.FileAttributes]::Directory -bor [IO.FileAttributes]::ReparsePoint;
            LinkType = 'Junction'; Target = @({ps_string(prefix + str(patch))}) }}
    }}
    return (& $script:RealPathInfo $Path)
}}
function Get-SetupChildPaths([string]$Path) {{
    if ($Path -eq {ps_string(alias)}) {{ throw 'alias must never be enumerated' }}
    if ($Path -eq {ps_string(patch)}) {{ $script:VisitedPatch += 1 }}
    return ,(& $script:RealChildPaths $Path)
}}
Assert-InstallTree {ps_string(cache)}
if ($script:VisitedPatch -ne 1) {{ throw 'patch must be inspected once, separately from alias' }}
Write-Output 'validated'
"""
    )
    assert output.strip() == "validated"


@pytest.mark.parametrize(
    "relative",
    [
        r".venv\Lib",
        r".venv\Lib\site-packages",
        r".venv\Lib\site-packages\package\nested",
        r".desktop-mcp-setup-cache\packages\package\nested",
        r".desktop-mcp-setup-cache\python\distribution\Lib",
    ],
)
def test_nested_reparse_denial_precedes_any_setup_process(owned: Path, relative: str) -> None:
    trace = owned / "unexpected-process.txt"
    injection = f"""
$script:RealPathInfo = (Get-Command Get-SetupPathInfo).ScriptBlock
$script:RealChildPaths = (Get-Command Get-SetupChildPaths).ScriptBlock
function Get-SetupPathInfo([string]$Path) {{
    if ($Path -eq (Join-Path $script:SetupRoot {ps_string(relative)})) {{
        return [pscustomobject]@{{ Attributes = [IO.FileAttributes]::Directory -bor [IO.FileAttributes]::ReparsePoint }}
    }}
    return (& $script:RealPathInfo $Path)
}}
function Get-SetupChildPaths([string]$Path) {{
    if ($Path -eq (Join-Path $script:SetupRoot {ps_string(relative)})) {{
        throw 'TEST FAILURE: followed a reparse point'
    }}
    return ,(& $script:RealChildPaths $Path)
}}
function Invoke-SetupProcess {{
    [IO.File]::WriteAllText({ps_string(trace)}, 'unexpected process')
    throw 'TEST FAILURE: started a process before validating descendants'
}}
function Get-UvExecutable {{ throw 'TEST FAILURE: bootstrapped before validating descendants' }}
"""
    install = make_install(owned, injection)
    # Ordinary owned directories only; metadata reports the reparse point. No
    # deletion, malicious path mutant, or real junction target is exercised.
    fake_link = install / relative
    fake_link.mkdir(parents=True)
    scripts = install / ".venv" / "Scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "python.exe").write_bytes(b"fixture, never executed")
    (scripts.parent / "pyvenv.cfg").write_text("fixture", encoding="utf-8")
    sentinel = fake_link / "keep.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    result = run_wrapper(install, owned, "-SkipCopilot -SkipShortcut")
    assert result.returncode != 0
    assert "linked/junction descendants" in result.stderr
    assert "TEST FAILURE" not in result.stdout + result.stderr
    assert not trace.exists()
    assert sentinel.read_text(encoding="utf-8") == "untouched"


@pytest.mark.parametrize("bound", ["entries", "depth", "time"])
def test_install_tree_walk_is_bounded_without_recursing_through_links(
    owned: Path, bound: str
) -> None:
    tree = owned / "tree"
    tree.mkdir()
    (tree / "one").mkdir()
    (tree / "two").mkdir()
    limits = {
        "entries": "$script:MaxTreeEntries = 2",
        "depth": "$script:MaxTreeDepth = 0",
        "time": "$script:MaxTreeSeconds = -1",
    }
    result = run_ps(load_setup() + limits[bound] + f"\nAssert-InstallTree {ps_string(tree)}")
    assert result.returncode != 0
    assert "inspection exceeded" in result.stderr
    assert "Nothing was synchronized" in result.stderr
    assert sorted(path.name for path in tree.iterdir()) == ["one", "two"]


def test_install_tree_scan_fails_closed_when_metadata_cannot_be_read(owned: Path) -> None:
    tree = owned / "tree"
    tree.mkdir()
    leaf = tree / "unreadable"
    leaf.mkdir()
    result = run_ps(
        load_setup()
        + f"""
$script:RealPathInfo = (Get-Command Get-SetupPathInfo).ScriptBlock
function Get-SetupPathInfo([string]$Path) {{
    if ($Path -eq {ps_string(leaf)}) {{ throw 'simulated metadata denial' }}
    return (& $script:RealPathInfo $Path)
}}
Assert-InstallTree {ps_string(tree)}
"""
    )
    assert result.returncode != 0
    assert "simulated metadata denial" in result.stderr


def test_junction_environment_is_rejected_without_following_it(owned: Path) -> None:
    install = make_install(owned)
    other = owned / "other-env"
    other.mkdir()
    sentinel = other / "keep.txt"
    sentinel.write_text("must survive", encoding="utf-8")
    result = run_ps(
        f"""
New-Item -ItemType Junction -Path {ps_string(install / ".venv")} -Target {ps_string(other)} | Out-Null
& {ps_string(install / "scripts" / "setup.ps1")} -SkipCopilot -SkipShortcut
"""
    )
    assert result.returncode != 0
    assert "linked/junction" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "must survive"
    assert not (install / ".desktop-mcp-setup-cache").exists()


def test_unowned_bootstrap_cache_is_not_overwritten(owned: Path) -> None:
    cache = owned / "cache"
    cache.mkdir()
    owner = cache / "owner.txt"
    owner.write_text("another application", encoding="utf-8")
    result = run_ps(load_setup() + f"Initialize-SetupCache {ps_string(cache)}")
    assert result.returncode != 0
    assert "ownership marker" in result.stderr
    assert owner.read_text(encoding="utf-8") == "another application"


def archive_fixture(owned: Path, entries: dict[str, bytes] | None = None) -> tuple[Path, str]:
    source = owned / "fixture.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in (entries or {"uv.exe": b"fixture uv; never executed"}).items():
            archive.writestr(name, content)
    return source, hashlib.sha256(source.read_bytes()).hexdigest()


def bootstrap_script(cache: Path, source: Path, checksum: str) -> str:
    return (
        load_setup()
        + f"""
$env:PATH = {ps_string(cache)}
$script:UvSha256 = {ps_string(checksum)}
function Receive-UvArchive([string]$Destination) {{
    [IO.File]::Copy({ps_string(source)}, $Destination)
}}
Get-UvExecutable {ps_string(cache)}
"""
    )


def test_no_python_or_uv_bootstrap_verifies_then_extracts_only_exact_executable(
    owned: Path,
) -> None:
    source, checksum = archive_fixture(
        owned,
        {"uv.exe": b"fixture uv; never executed", "uvx.exe": b"unused", "../escape.exe": b"unused"},
    )
    cache = owned / "cache"
    cache.mkdir()
    ps_ok(bootstrap_script(cache, source, checksum), cwd=owned)
    executable = cache / "uv-0.12.10.exe"
    assert executable.read_bytes() == b"fixture uv; never executed"
    assert not (owned / "escape.exe").exists()
    assert not (cache / "uvx.exe").exists()
    executable.write_bytes(b"tampered executable")
    ps_ok(bootstrap_script(cache, source, checksum), cwd=owned)
    assert executable.read_bytes() == b"fixture uv; never executed"
    assert len(list(cache.glob("*.zip"))) == 1


def test_existing_uv_is_preferred_without_downloading_or_executing_it(owned: Path) -> None:
    existing = owned / "existing uv.exe"
    existing.write_bytes(b"not executable; discovery fixture")
    output = ps_ok(
        load_setup()
        + f"""
function Find-UvExecutable {{ return {ps_string(existing)} }}
function Read-UvVersion {{ return 'uv 0.12.10 (fixture)' }}
function Receive-UvArchive {{ throw 'must not download' }}
Get-UvExecutable {ps_string(owned / "unused cache")}
"""
    )
    assert output.strip() == str(existing)
    assert not (owned / "unused cache").exists()


@pytest.mark.parametrize(
    "text,compatible",
    [
        ("uv 0.2.37", False),
        ("uv 0.12.9", False),
        ("uv 0.12.10 (fixture)", True),
        ("uv 0.13.0", True),
        ("uv 0.12.10.dev1", False),
        ("not uv", False),
        ("", False),
    ],
)
def test_existing_uv_version_floor_is_explicit(owned: Path, text: str, compatible: bool) -> None:
    output = ps_ok(
        load_setup()
        + f"""
function Read-UvVersion {{ return {ps_string(text)} }}
Test-UvCompatibility {ps_string(owned / "fake uv.exe")} | ConvertTo-Json -Compress
"""
    )
    assert json.loads(output) is compatible


def test_unusable_uv_version_probe_selects_fallback(owned: Path) -> None:
    output = ps_ok(
        load_setup()
        + f"""
function Read-UvVersion {{ throw 'simulated launch failure' }}
Test-UvCompatibility {ps_string(owned / "fixture uv.exe")} | ConvertTo-Json -Compress
"""
    )
    assert json.loads(output) is False


def test_old_uv_falls_back_to_verified_local_binary_without_changing_global(owned: Path) -> None:
    source, checksum = archive_fixture(owned)
    global_uv = owned / "global uv.exe"
    before = b"old global uv fixture; never executed"
    global_uv.write_bytes(before)
    cache = owned / "cache"
    cache.mkdir()
    output = ps_ok(
        load_setup()
        + f"""
$script:UvSha256 = {ps_string(checksum)}
function Find-UvExecutable {{ return {ps_string(global_uv)} }}
function Read-UvVersion {{ return 'uv 0.2.37' }}
function Receive-UvArchive([string]$Destination) {{
    [IO.File]::Copy({ps_string(source)}, $Destination)
}}
Get-UvExecutable {ps_string(cache)}
"""
    )
    assert "Existing uv is incompatible" in output
    assert str(cache / "uv-0.12.10.exe") in output
    assert (cache / "uv-0.12.10.exe").read_bytes() == b"fixture uv; never executed"
    assert global_uv.read_bytes() == before


@pytest.mark.parametrize("mode", ["timeout", "exit-failure", "oversized", "success"])
def test_uv_version_probe_is_bounded_and_only_stops_its_owned_process(
    owned: Path, mode: str
) -> None:
    text = "x" * 513 if mode == "oversized" else "uv 0.12.10"
    finished = "$false" if mode == "timeout" else "$true"
    exit_code = 9 if mode == "exit-failure" else 0
    output = ps_ok(
        load_setup()
        + f"""
$script:ProbeEvents = New-Object 'Collections.Generic.List[string]'
function Start-UvVersionProcess($Info) {{
    if ($Info.Arguments -ne '"--version"' -or -not $Info.RedirectStandardOutput -or -not $Info.RedirectStandardError) {{
        throw 'unexpected probe command'
    }}
    $process = [pscustomobject]@{{ Id = 424242; ExitCode = {exit_code};
        StandardOutput = [IO.StringReader]::new({ps_string(text)}) }}
    $process | Add-Member ScriptMethod WaitForExit {{
        param($milliseconds)
        $script:ProbeEvents.Add("wait:$milliseconds")
        return {finished}
    }}
    $process | Add-Member ScriptMethod Dispose {{ $script:ProbeEvents.Add('disposed') }}
    return $process
}}
function Stop-Process {{
    [CmdletBinding()]
    param([int]$Id)
    $script:ProbeEvents.Add("stop:$Id")
}}
$value = Read-UvVersion {ps_string(owned / "fixture uv.exe")}
@{{ value = $value; events = @($script:ProbeEvents) }} | ConvertTo-Json -Compress
"""
    )
    result = json.loads(output)
    assert result["value"] == ("uv 0.12.10" if mode == "success" else None)
    assert result["events"] == (
        ["wait:5000", "stop:424242", "disposed"] if mode == "timeout" else ["wait:5000", "disposed"]
    )


@pytest.mark.parametrize("cached", [False, True])
def test_wrong_download_or_cached_checksum_never_creates_an_executable(
    owned: Path, cached: bool
) -> None:
    source, _ = archive_fixture(owned)
    cache = owned / "cache"
    cache.mkdir()
    if cached:
        (cache / "uv-0.12.10.zip").write_bytes(source.read_bytes())
    result = run_ps(bootstrap_script(cache, source, "0" * 64))
    assert result.returncode != 0
    assert "SHA256" in result.stderr
    assert not list(cache.glob("*.exe"))


@pytest.mark.parametrize("failure", ["network", "size", "scheme", "stream-limit"])
def test_download_failures_are_bounded_and_explicit(owned: Path, failure: str) -> None:
    destination = owned / "owned.download"
    if failure == "network":
        mock = "function Open-UvDownload { throw 'simulated network failure' }"
    else:
        length = 100 if failure == "size" else -1
        scheme = "http" if failure == "scheme" else "https"
        mock = f"""
$script:MaxArchiveBytes = 16
function Open-UvDownload {{
    $response = [pscustomobject]@{{
        ResponseUri = [Uri]'{scheme}://example.invalid/fixture'; ContentLength = {length}
        Body = [IO.MemoryStream]::new([byte[]](1..32))
    }}
    $response | Add-Member ScriptMethod GetResponseStream {{ return $this.Body }}
    $response | Add-Member ScriptMethod Dispose {{ }}
    return $response
}}
"""
    result = run_ps(load_setup() + mock + f"\nReceive-UvArchive {ps_string(destination)}")
    assert result.returncode != 0
    assert (
        "simulated network failure" in result.stderr
        if failure == "network"
        else "limit" in result.stderr
    )
    assert not destination.exists() or destination.stat().st_size <= 16


@pytest.mark.parametrize("invalid", ["missing-executable", "entry-count", "executable-size"])
def test_archive_layout_and_extraction_bounds(owned: Path, invalid: str) -> None:
    entries = {"wrong.exe": b"unused"} if invalid == "missing-executable" else {"uv.exe": b"small"}
    if invalid == "entry-count":
        entries.update({f"unused{i}": b"x" for i in range(8)})
    source, checksum = archive_fixture(owned, entries)
    cache = owned / "cache"
    cache.mkdir()
    code = bootstrap_script(cache, source, checksum)
    if invalid == "executable-size":
        # Exercise the actual streaming cap without constructing a large archive.
        code = (
            load_setup()
            + f"""
$inputStream = [IO.MemoryStream]::new([byte[]](1..32))
$outputStream = [IO.File]::Open({ps_string(cache / "bounded.fixture")}, 'CreateNew', 'Write', 'None')
try {{ Copy-BoundedStream $inputStream $outputStream 16 }}
finally {{ $inputStream.Dispose(); $outputStream.Dispose() }}
"""
        )
    result = run_ps(code)
    assert result.returncode != 0
    assert "layout or size" in result.stderr or "size or time limit" in result.stderr
    assert not list(cache.glob("*.exe"))


def test_stream_time_limit_is_enforced(owned: Path) -> None:
    result = run_ps(
        load_setup()
        + """
$inputStream = [IO.MemoryStream]::new([byte[]](1..2))
$outputStream = [IO.MemoryStream]::new()
try { Copy-BoundedStream $inputStream $outputStream 100 -Seconds -1 }
finally { $inputStream.Dispose(); $outputStream.Dispose() }
"""
    )
    assert result.returncode != 0
    assert "time limit" in result.stderr


@pytest.mark.parametrize("foreign", [False, True])
def test_existing_launcher_keeps_its_shortcut_conflict_guard_without_com_or_gui(
    owned: Path, monkeypatch: pytest.MonkeyPatch, foreign: bool
) -> None:
    import platformdirs
    import pythoncom
    from PIL import Image
    import win32com.client
    from win32com.shell import shell

    from desktop_mcp import cursor, launcher

    programs = owned / "Programs"
    programs.mkdir()
    shortcut_path = programs / "Desktop-MCP.lnk"
    shortcut_path.write_text("existing fixture shortcut", encoding="utf-8")
    executable = owned / "desktop-mcp-ui.exe"
    executable.write_bytes(b"fixture; never executed")
    target = owned / "other.exe" if foreign else executable
    shortcut = SimpleNamespace(
        TargetPath=str(target),
        Save=lambda: shortcut_path.write_text("updated fixture shortcut", encoding="utf-8"),
    )
    notifications = []
    monkeypatch.setattr(launcher.sys, "executable", str(owned / "python.exe"))
    monkeypatch.setattr(platformdirs, "user_data_dir", lambda *args, **kwargs: str(owned / "data"))
    monkeypatch.setattr(pythoncom, "CoInitialize", lambda: None)
    monkeypatch.setattr(pythoncom, "CoUninitialize", lambda: None)
    monkeypatch.setattr(shell, "SHGetFolderPath", lambda *args: str(programs))
    monkeypatch.setattr(shell, "SHChangeNotify", lambda *args: notifications.append(args))
    monkeypatch.setattr(
        win32com.client,
        "Dispatch",
        lambda name: SimpleNamespace(CreateShortcut=lambda path: shortcut),
    )
    monkeypatch.setattr(
        cursor, "render_cursor", lambda **kwargs: SimpleNamespace(image=Image.new("RGBA", (2, 2)))
    )
    if foreign:
        with pytest.raises(FileExistsError, match="not overwritten"):
            launcher.install_shortcut()
        assert shortcut_path.read_text(encoding="utf-8") == "existing fixture shortcut"
        assert not (owned / "data").exists()
        assert not notifications
    else:
        assert launcher.install_shortcut() == shortcut_path
        assert shortcut.TargetPath == str(executable)
        assert shortcut.WorkingDirectory == str(owned)
        assert shortcut.Arguments == ""
        assert notifications
