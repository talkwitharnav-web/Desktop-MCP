"""An idle child process proves job ownership, without starting a desktop host."""

import json
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
import win32api
import win32con
import win32event
import win32job


@pytest.mark.parametrize("allow_breakaway", [False, True])
def test_host_lifetime_is_not_owned_by_a_clients_kill_on_close_job(allow_breakaway):
    packages = str(Path(sys.prefix) / "Lib" / "site-packages")
    script = f"""
import sys
sys.stdin.buffer.read(1)
import site
site.addsitedir({packages!r})
import json
from pathlib import Path
import subprocess
from desktop_mcp import service
original = subprocess.Popen
def idle_host(arguments, **kwargs):
    return original([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
service.subprocess.Popen = idle_host
try:
    process = service._spawn_host(Path(sys.executable))
except RuntimeError as error:
    print(json.dumps({{'blocked': True, 'message': str(error)}}), flush=True)
else:
    print(json.dumps({{'pid': process.pid}}), flush=True)
"""
    job = win32job.CreateJobObject(None, f"Local\\Desktop-MCP-test-job-{uuid.uuid4().hex}")
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info["BasicLimitInformation"]["LimitFlags"] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if allow_breakaway:
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_BREAKAWAY_OK
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    helper = subprocess.Popen(
        [sys._base_executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    descendant = None
    try:
        win32job.AssignProcessToJobObject(job, int(helper._handle))
        stdout, stderr = helper.communicate(b"go", timeout=15)
        assert helper.returncode == 0, stderr.decode("utf-8", errors="replace")
        result = json.loads(stdout)
        if not allow_breakaway:
            assert result["blocked"]
            assert "Start first" in result["message"]
        elif result.get("blocked"):
            pytest.skip("An enclosing Windows job disallows breakaway even from the test job.")
        else:
            descendant = win32api.OpenProcess(
                win32con.SYNCHRONIZE | win32con.PROCESS_TERMINATE, False, result["pid"]
            )
            job.Close()
            job = None
            assert win32event.WaitForSingleObject(descendant, 100) == win32event.WAIT_TIMEOUT
    finally:
        if descendant is not None:
            if win32event.WaitForSingleObject(descendant, 0) == win32event.WAIT_TIMEOUT:
                win32api.TerminateProcess(descendant, 0)
                win32event.WaitForSingleObject(descendant, 3000)
            descendant.Close()
        if job is not None:
            job.Close()
        if helper.poll() is None:
            helper.kill()
            helper.wait(timeout=3)
