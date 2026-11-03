"""
Type-check RTAC user logic by compiling it in a standalone CODESYS install.

AcSELerator's `AcRtacCmd check` reports success even when the logic has
compile errors, so instead the export's POUs / GVLs / DataTypes are copied
verbatim into a fresh CODESYS project, alongside generated SEL type stubs and
device-tag globals, and built headless. Object names and line numbers in the
results match the RTAC source because the text is copied unchanged.
"""

import datetime
import json
import os
import re
import subprocess
import tempfile

from . import rtac_xml, sel_stubs

HERE = os.path.dirname(os.path.abspath(__file__))
JOB_SCRIPT = os.path.join(HERE, "codesys_scripts", "compile_job.py")

CODESYS_PATH = os.environ.get(
    "CODESYS_PATH", r"C:\Program Files\CODESYS 3.5.22.40\CODESYS\Common\CODESYS.exe")
CODESYS_PROFILE = os.environ.get("CODESYS_PROFILE", "CODESYS V3.5 SP22 Patch 4")
CODESYS_DEVICE = os.environ.get("CODESYS_DEVICE", "CODESYS Control Win V3 x64")
STANDARD_LIBRARY = os.environ.get("CODESYS_STANDARD_LIBRARY", "Standard, 3.5.22.0 (System)")
WORK_ROOT = os.environ.get("RTAC_MCP_WORK_DIR", os.path.join(tempfile.gettempdir(), "rtac-mcp"))

HARNESS = "RtacMcp_InstantiateAll"

_POSITION = re.compile(r"Line (\d+), Column (\d+) \((\w+)\)")
_FUNC_RETURN = re.compile(r"^\s*FUNCTION\s+\w+\s*:\s*([A-Za-z_][\w.]*(?:\s*\([^)]*\))?)", re.I | re.M)


def _user_objects(export_dir):
    objs = []
    for o in rtac_xml.list_objects(export_dir):
        if o["kind"] == "DataType":
            objs.append({"kind": "dut", "name": o["name"], "text": o["content"]})
        elif o["kind"] == "GVL":
            objs.append({"kind": "gvl", "name": o["name"], "text": o["content"]})
        else:
            job = {"kind": "pou", "name": o["name"], "pou_type": o["pou_kind"],
                   "decl": o["interface"], "impl": o["implementation"]}
            if o["pou_kind"] == "Function":
                m = _FUNC_RETURN.search(o["interface"])
                job["return_type"] = m.group(1) if m else "BOOL"
            objs.append(job)
    return objs


def build_job(export_dir, work_dir):
    user = _user_objects(export_dir)
    user_names = {o["name"].lower() for o in user}
    stubs = [o for o in sel_stubs.library_objects() if o["name"].lower() not in user_names]
    tag_objs, unknown_types = sel_stubs.tag_objects(
        rtac_xml.list_tags(export_dir), rtac_xml.list_devices(export_dir),
        rtac_xml.declared_globals(export_dir) | user_names)
    stubs += [o for o in tag_objs if o["name"].lower() not in user_names]

    # CODESYS (like RTAC) skips POUs nothing references, so a new FB would go
    # unchecked until something instantiates it. Declare one of each.
    fbs = [o["name"] for o in user if o.get("pou_type") == "FunctionBlock"]
    programs = [o["name"] for o in user if o.get("pou_type") == "Program"]
    if fbs:
        decls = "".join("\t_%d : %s;\n" % (i, n) for i, n in enumerate(fbs))
        stubs.append({"kind": "pou", "name": HARNESS, "pou_type": "Program",
                      "decl": "PROGRAM %s\nVAR\n%sEND_VAR\n" % (HARNESS, decls), "impl": ""})
        programs.append(HARNESS)

    job = {
        "project_path": os.path.join(work_dir, "rtac_check.project"),
        "result_path": os.path.join(work_dir, "result.json"),
        "device_name": CODESYS_DEVICE,
        # RTAC logic uses IEC timers/triggers/bistables (TON, R_TRIG, RS ...).
        "libraries": [STANDARD_LIBRARY],
        "objects": user + stubs,
        "task_programs": programs,
    }
    return job, user_names, unknown_types


def _run_codesys(job, work_dir, timeout):
    job_path = os.path.join(work_dir, "job.json")
    with open(job_path, "w") as f:
        json.dump(job, f, indent=2)
    with open(JOB_SCRIPT) as f:
        script = f.read().replace("__JOB_PATH__", job_path)
    script_path = os.path.join(work_dir, "run_compile.py")
    with open(script_path, "w") as f:
        f.write(script)
    # CODESYS parses its own command line, so pass a single pre-quoted string.
    cmd = '"%s" --profile="%s" --noUI --runscript="%s"' % (CODESYS_PATH, CODESYS_PROFILE, script_path)
    proc = subprocess.run(cmd, cwd=os.path.dirname(CODESYS_PATH), capture_output=True,
                          text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    if not os.path.exists(job["result_path"]):
        raise RuntimeError("CODESYS produced no result (exit %s).\n%s\n%s"
                           % (proc.returncode, proc.stdout[-2000:], proc.stderr[-2000:]))
    with open(job["result_path"]) as f:
        return json.load(f)


def compile_export(export_dir, timeout=900):
    export_dir = os.path.abspath(export_dir)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    work_dir = os.path.join(WORK_ROOT, stamp)
    os.makedirs(work_dir, exist_ok=True)

    job, user_names, unknown_types = build_job(export_dir, work_dir)
    raw = _run_codesys(job, work_dir, timeout)
    if raw.get("fatal"):
        return {"ok": False, "error": "CODESYS script failed", "detail": raw["fatal"], "work_dir": work_dir}

    issues = []
    for m in raw["messages"]:
        if m.get("severity") not in ("Error", "Warning") or m.get("category") != "Build":
            continue
        issue = {"severity": m["severity"].lower(), "message": m.get("text", ""),
                 "code": "%s%s" % (m.get("prefix", ""), m.get("number", "")) if m.get("number") else None}
        obj = m.get("object")
        if obj:
            issue["object"] = obj
            issue["source"] = "user" if obj.lower() in user_names else "stub"
        pos = _POSITION.search(m.get("position_text") or "")
        if pos:
            issue["line"], issue["column"] = int(pos.group(1)), int(pos.group(2))
            issue["section"] = {"Impl": "implementation", "Decl": "interface"}.get(pos.group(3), pos.group(3))
        issues.append(issue)

    errors = [i for i in issues if i["severity"] == "error"]
    result = {
        "ok": not errors and not raw["create_errors"],
        "error_count": len(errors),
        "warning_count": len(issues) - len(errors),
        "issues": issues,
        "objects_compiled": len(user_names),
        "work_dir": work_dir,
        "note": ("Compiled in CODESYS against approximate SEL stubs. Errors with source 'user' are in "
                 "RTAC logic; 'stub' errors mean a stub in tools/sel_stubs.py needs extending."),
    }
    if raw["create_errors"]:
        result["create_errors"] = raw["create_errors"]
    if unknown_types:
        result["unstubbed_tag_types"] = unknown_types
    return result
