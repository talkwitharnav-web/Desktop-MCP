"""Conservatively register this installation; never display configuration contents."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from uuid import uuid4


class ConfigError(ValueError):
    """An unsafe or conflicting configuration must be left unchanged."""


class RecoveryRequired(ConfigError):
    """A previous commit is incomplete; never interpret a missing target as new."""


def plain_path(path: Path) -> None:
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ConfigError("Linked/junction configuration paths are not supported.")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ConfigError("Hard-linked configuration files are not supported.")


def read_state(path: Path) -> tuple[bytes | None, tuple[int, ...] | None]:
    plain_path(path)
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            content = stream.read(4 * 1024 * 1024 + 1)
            after = os.fstat(stream.fileno())
    except FileNotFoundError:
        return None, None

    def signature(info: os.stat_result) -> tuple[int, ...]:
        # Windows stat/fstat disagree about ctime (creation vs. change time).
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    if len(content) > 4 * 1024 * 1024:
        raise ConfigError("Copilot configuration exceeds the 4 MiB safety limit.")
    if signature(before) != signature(after) or signature(path.stat()) != signature(after):
        raise ConfigError(
            "Copilot configuration changed while being read; retry when editing is finished."
        )
    return content, signature(after)


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("Copilot JSON contains duplicate keys; nothing was reset.")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise ConfigError("Copilot configuration must contain standard JSON values.")


def merged_config(content: bytes | None, python: Path) -> tuple[dict, bool]:
    try:
        data = (
            json.loads(
                content.decode("utf-8-sig"),
                object_pairs_hook=unique_object,
                parse_constant=reject_constant,
            )
            if content is not None
            else {}
        )
    except (UnicodeError, ValueError) as error:
        raise ConfigError(
            "Copilot configuration is not valid UTF-8 JSON; nothing was reset."
        ) from error
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise ConfigError("Copilot configuration and mcpServers must be JSON objects.")
    servers = data.get("mcpServers", {})
    args = ["-m", "desktop_mcp", "serve"]
    entry = servers.get("desktop-mcp", {})
    if "desktop-mcp" in servers:
        if (
            not isinstance(entry, dict)
            or entry.get("type", "local") != "local"
            or not isinstance(entry.get("command"), str)
            or not Path(entry["command"]).is_absolute()
            or os.path.normcase(os.path.normpath(entry["command"]))
            != os.path.normcase(os.path.normpath(str(python)))
            or entry.get("args") != args
        ):
            raise ConfigError(
                "desktop-mcp already names a different command/installation. "
                "It was not overwritten; resolve it manually or use -SkipCopilot."
            )
        if "tools" in entry and (
            not isinstance(entry["tools"], list)
            or not all(isinstance(tool, str) for tool in entry["tools"])
        ):
            raise ConfigError(
                "The existing desktop-mcp tools restriction is invalid; it was not changed."
            )
    updated = dict(entry)
    timeout = entry.get("timeout", 45000)
    if type(timeout) is not int or timeout <= 0 or timeout > 2**31 - 1:
        raise ConfigError("desktop-mcp timeout must be a positive integer number of milliseconds.")
    updated.update(type="local", command=str(python), args=args, timeout=max(45000, timeout))
    updated.setdefault("tools", ["*"])
    result = {**data, "mcpServers": {**servers, "desktop-mcp": updated}}
    return result, result != data


def protected_security(config: Path | None, original: tuple | None = None) -> object:
    """Copy the source object's permissions, or create an owner-only descriptor."""
    import msvcrt
    import ntsecuritycon
    import pywintypes
    import win32api
    import win32security

    if config is not None:
        # Read the descriptor from the same open object as the original bytes,
        # not a second pathname lookup that could select another file's ACL.
        with config.open("rb") as stream:
            before = os.fstat(stream.fileno())
            descriptor = win32security.GetSecurityInfo(
                msvcrt.get_osfhandle(stream.fileno()),
                win32security.SE_FILE_OBJECT,
                win32security.OWNER_SECURITY_INFORMATION
                | win32security.GROUP_SECURITY_INFORMATION
                | win32security.DACL_SECURITY_INFORMATION,
            )
            content = stream.read(4 * 1024 * 1024 + 1)
            signature = before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
            if original != (content, signature):
                raise ConfigError("Copilot configuration changed before securing the update.")
        if descriptor.GetSecurityDescriptorDacl() is None:
            return protected_security(None)
    else:
        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
        )
        try:
            owner = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            token.Close()
        acl = win32security.ACL()
        acl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, owner)
        descriptor = win32security.SECURITY_DESCRIPTOR()
        descriptor.SetSecurityDescriptorOwner(owner, False)
        descriptor.SetSecurityDescriptorDacl(True, acl, False)
    descriptor.SetSecurityDescriptorControl(
        win32security.SE_DACL_PROTECTED, win32security.SE_DACL_PROTECTED
    )
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    attributes.bInheritHandle = False
    return attributes


def acl_entries(descriptor: object) -> tuple | None:
    import win32security

    acl = descriptor.GetSecurityDescriptorDacl()
    if acl is None:
        return None
    entries = []
    for index in range(acl.GetAceCount()):
        header, *body = acl.GetAce(index)
        # Windows may clear the informational inherited flag on a protected copy.
        entries.append(((header[0], header[1] & ~win32security.INHERITED_ACE), *body))
    return tuple(entries)


def write_protected(path: Path, payload: bytes, security: object) -> None:
    """Establish and verify the DACL at CREATE_NEW, before the first payload write."""
    import win32con
    import win32file
    import win32security

    plain_path(path)
    handle = win32file.CreateFile(
        str(path),
        win32con.GENERIC_READ | win32con.GENERIC_WRITE,
        0,
        security,
        win32con.CREATE_NEW,
        win32con.FILE_ATTRIBUTE_NORMAL | win32file.FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    try:
        actual = win32security.GetSecurityInfo(
            handle, win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
        )
        if (
            not actual.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED
            or acl_entries(actual) is None
            or acl_entries(actual) != acl_entries(security.SECURITY_DESCRIPTOR)
        ):
            raise ConfigError(
                "Protected file permissions could not be established; no payload was written."
            )
        offset = 0
        while offset < len(payload):
            _, written = win32file.WriteFile(handle, payload[offset:])
            if written <= 0:
                raise OSError("Incomplete protected write")
            offset += written
        win32file.FlushFileBuffers(handle)
    finally:
        handle.Close()


def recovery_error(record: Path) -> RecoveryRequired:
    return RecoveryRequired(
        f"Copilot recovery required: unfinished setup record {record.name}. "
        "If another setup is running, wait for it. Otherwise reconcile any protected "
        ".original backup and .pending file with any current configuration manually; "
        "only then rename this .recovery record to .resolved. No automatic restore was attempted."
    )


def check_recovery(config: Path) -> None:
    """Read only: an unfinished/invalid recovery record blocks even an absent config."""
    plain_path(config.parent)
    if not config.parent.exists():
        return
    prefix = os.path.normcase(config.name + ".desktop-mcp-")
    with os.scandir(config.parent) as entries:
        for count, entry in enumerate(entries, 1):
            if count > 10000:
                raise RecoveryRequired(
                    "Copilot recovery scan limit reached; inspect the configuration directory."
                )
            name = os.path.normcase(entry.name)
            if name.startswith(prefix) and name.endswith(".recovery"):
                record = config.parent / entry.name
                plain_path(record)
                complete = record.with_suffix(".complete")
                plain_path(complete)
                try:
                    with complete.open("rb") as stream:
                        committed = stream.read(32) == b"committed\n"
                except FileNotFoundError:
                    committed = False
                if not committed:
                    raise recovery_error(record)


def replace_config(staged: Path, target: Path, existed: bool) -> None:
    if not existed:
        # On Windows rename fails if another writer created the destination.
        os.rename(staged, target)
        return
    # Preserve the final target ACL. The independently flushed, protected
    # .original snapshot is NOT handed to ReplaceFile: its documented 1176/1177
    # partial renames must not be able to consume our recovery copy.
    replace = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
    replace.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    replace.restype = wintypes.BOOL
    if not replace(str(target), str(staged), None, 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def configure(config: Path, python: Path, *, check: bool = False) -> bool:
    """Merge one entry under a setup lock, with a final optimistic conflict check."""
    import msvcrt
    import pywintypes

    if not config.is_absolute() or not python.is_absolute() or not python.is_file():
        raise ConfigError(
            "An absolute configuration path and existing absolute Python path are required."
        )
    check_recovery(config)
    original = read_state(config)
    _, changed = merged_config(original[0], python)
    if check or not changed:
        return changed
    config.parent.mkdir(parents=True, exist_ok=True)
    lock_path = config.with_name(config.name + ".desktop-mcp-setup.lock")
    plain_path(lock_path)
    with lock_path.open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise ConfigError(
                "Another setup is updating this Copilot configuration; retry later."
            ) from error
        try:
            check_recovery(config)
            original = read_state(config)
            data, changed = merged_config(original[0], python)
            if not changed:
                return False
            payload = (
                json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
            ).encode("utf-8")
            if len(payload) > 4 * 1024 * 1024:
                raise ConfigError("Merged Copilot configuration exceeds the 4 MiB safety limit.")
            stem = config.name + ".desktop-mcp-" + uuid4().hex
            staged = config.with_name(stem + ".pending")
            backup = config.with_name(stem + ".original")
            record = config.with_name(stem + ".recovery")
            security = (
                protected_security(config, original)
                if original[0] is not None
                else protected_security(None)
            )
            record_security = protected_security(None)
            write_protected(staged, payload, security)
            if original[0] is not None:
                write_protected(backup, original[0], security)
            if read_state(config) != original:
                raise ConfigError(
                    "Copilot configuration changed before replacement; nothing was overwritten."
                )
            metadata = {
                "version": 1,
                "config": config.name,
                "original": backup.name if original[0] is not None else None,
                "pending": staged.name,
                "original_sha256": hashlib.sha256(original[0]).hexdigest()
                if original[0] is not None
                else None,
                "pending_sha256": hashlib.sha256(payload).hexdigest(),
            }
            write_protected(record, (json.dumps(metadata) + "\n").encode("utf-8"), record_security)
            # Record and backup are durable before the possibly partial native
            # operation. Never guess whether 1176/1177 committed, nor overwrite
            # a concurrent writer by attempting an automatic rollback.
            try:
                if read_state(config) != original:
                    raise ConfigError(
                        "Copilot configuration changed immediately before replacement."
                    )
                replace_config(staged, config, original[0] is not None)
                completion = record.with_suffix(".completion-pending")
                write_protected(completion, b"committed\n", record_security)
                # Never publish success before the secured write/flush/close
                # finishes: native failures can leave a fully populated file.
                os.rename(completion, record.with_suffix(".complete"))
            except (OSError, ConfigError, pywintypes.error) as error:
                if getattr(error, "winerror", None) in (1176, 1177):
                    raise RecoveryRequired(
                        f"Windows replacement failed ({error.winerror}); configuration may be missing or renamed. "
                        f"{recovery_error(record)}"
                    ) from error
                raise recovery_error(record) from error
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="Validate without writing anything.")
    options = parser.parse_args()
    try:
        import pywintypes
    except ImportError:
        print(
            "Required Windows security APIs are unavailable; no configuration update was attempted.",
            file=sys.stderr,
        )
        return 1
    try:
        changed = configure(options.config, options.python, check=options.check)
    except ConfigError as error:
        print(f"Copilot setup: {error}", file=sys.stderr)
        return 1
    except OSError, ValueError, ImportError, pywintypes.error:
        print(
            "Copilot setup did not finish. Check permissions and concurrent editors. "
            "If a .recovery record exists, resolve it before retrying; configuration may require recovery. "
            "No automatic reset or restore was attempted.",
            file=sys.stderr,
        )
        return 1
    print(
        "Copilot configuration checked (no writes)."
        if options.check
        else "Copilot desktop-mcp configured."
        if changed
        else "Copilot desktop-mcp already configured."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
