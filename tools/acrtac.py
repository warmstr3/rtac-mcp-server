"""
Thin wrapper around AcSELerator RTAC's command-line tool, AcRtacCmd.exe.

Every AcRtacCmd command ends with a status line "<command>:<code>:<status>".
Commands run against a background RTAC.exe process identified by an alias;
that process has to be logged in to the AcRTAC database before project
commands work. Login needs the user's database credentials: either set
RTAC_DB_USER / RTAC_DB_PASSWORD in the server's environment, or log the
process in by hand (see `login_hint`).

Findings this module relies on (AcSELerator 1.157):
  * importxml always creates a NEW project; it cannot update one in place.
  * delete fails while a project is open, so close first.
  * `check` returns success even when the logic has compile errors, so it is
    not exposed; use codesys_compile instead.
"""

import os
import re
import subprocess

ACRTACCMD = os.environ.get("ACRTACCMD_PATH", r"C:\Program Files\SEL\AcSELerator\RTAC\AcRtacCmd.exe")
ALIAS = os.environ.get("RTAC_ALIAS", "rtacmcp")
# Projects this server may delete must start with this prefix.
SCRATCH_PREFIX = os.environ.get("RTAC_SCRATCH_PREFIX", "MCP_")

_STATUS = re.compile(r"^(\w+):(\d+):(\w[\w ]*)\s*$", re.M)


class AcRtacError(RuntimeError):
    pass


def run(*args, timeout=900):
    # stdin must not be inherited: under MCP it is the protocol pipe, and
    # AcRtacCmd blocks reading it.
    proc = subprocess.run([ACRTACCMD, *args], capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)
    out = (proc.stdout or "") + (proc.stderr or "")
    statuses = _STATUS.findall(out)
    code = int(statuses[-1][1]) if statuses else proc.returncode
    return {"ok": code == 0, "code": code, "output": out.strip()}


def _check(res, what):
    if not res["ok"]:
        lines = [l for l in res["output"].splitlines()
                 if l.strip() and not l.startswith("\tat ") and l.strip() != "Call Stack:"]
        raise AcRtacError("%s failed: %s" % (what, "\n".join(lines[-6:])))
    return res


def login_hint():
    return ('& "%s" login -a %s -p "<password>" <username>' % (ACRTACCMD, ALIAS))


def status():
    info = run("info", "-a", ALIAS, timeout=60)
    if not info["ok"]:
        return {"process": "not running", "alias": ALIAS}
    lst = run("list", "-a", ALIAS, timeout=120)
    logged_in = lst["ok"]
    return {"process": "running", "alias": ALIAS, "logged_in": logged_in,
            "info": info["output"].splitlines()[-3:-1] if info["output"] else [],
            **({} if logged_in else {"login_command": login_hint()})}


def ensure_ready():
    """Start the RTAC process if needed and make sure it is logged in."""
    if not run("info", "-a", ALIAS, timeout=60)["ok"]:
        _check(run("start", "-a", ALIAS, timeout=300), "Starting RTAC process")
    if run("list", "-a", ALIAS, timeout=120)["ok"]:
        return
    user = os.environ.get("RTAC_DB_USER")
    if user:
        args = ["login", "-a", ALIAS]
        if os.environ.get("RTAC_DB_PASSWORD"):
            args += ["-p", os.environ["RTAC_DB_PASSWORD"]]
        _check(run(*args, user, timeout=120), "Logging in to AcRTAC database")
        return
    raise AcRtacError(
        "RTAC process '%s' is not logged in. Set RTAC_DB_USER/RTAC_DB_PASSWORD for the server, "
        "or run this in PowerShell and retry:\n%s" % (ALIAS, login_hint()))


def list_projects():
    ensure_ready()
    res = _check(run("list", "-a", ALIAS, timeout=120), "Listing projects")
    projects = []
    for line in res["output"].splitlines():
        line = line.strip()
        if not line or line.startswith(("|", "-", "list:")):
            continue
        name, ptype, version = line.rsplit(":", 2)
        projects.append({"name": name, "type": ptype, "version": version})
    return projects


def _project(name):
    for p in list_projects():
        if p["name"] == name:
            return p
    return None


def close():
    run("close", "-a", ALIAS, timeout=120)


def export_project(name, output_dir):
    ensure_ready()
    if _project(name) is None:
        raise AcRtacError("No project named '%s'" % name)
    output_dir = os.path.abspath(output_dir)
    if os.path.isdir(output_dir) and os.listdir(output_dir):
        raise AcRtacError("Output folder is not empty: %s" % output_dir)
    try:
        _check(run("exportxml", "-a", ALIAS, "-n", name, output_dir), "Exporting '%s'" % name)
    finally:
        close()
    return {"project": name, "export_dir": output_dir}


def import_project(name, device_type, version, export_dir):
    if not name.startswith(SCRATCH_PREFIX):
        raise AcRtacError("Imported project names must start with '%s' so they are clearly scratch "
                          "copies (got '%s')" % (SCRATCH_PREFIX, name))
    ensure_ready()
    if _project(name) is not None:
        raise AcRtacError("A project named '%s' already exists; importxml only creates new projects" % name)
    try:
        res = _check(run("importxml", "-a", ALIAS, "-n", name, str(device_type), version,
                         os.path.abspath(export_dir)), "Importing '%s'" % name)
    finally:
        close()
    warnings = [l for l in res["output"].splitlines() if l.startswith(("Warning", "Error"))]
    return {"project": name, "imported": True, "warnings": warnings}


def delete_project(name):
    if not name.startswith(SCRATCH_PREFIX):
        raise AcRtacError("Refusing to delete '%s': this server only deletes projects named '%s*'"
                          % (name, SCRATCH_PREFIX))
    ensure_ready()
    if _project(name) is None:
        raise AcRtacError("No project named '%s'" % name)
    close()
    _check(run("delete", "-a", ALIAS, name, timeout=300), "Deleting '%s'" % name)
    return {"project": name, "deleted": True}
