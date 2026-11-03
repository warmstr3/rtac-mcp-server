# -*- coding: utf-8 -*-
"""
Runs INSIDE CODESYS (IronPython 2.7) via --runscript.

Reads a JSON job, creates a fresh project with the job's device, adds the
job's DUTs / GVLs / POUs to the Application, assigns programs to the MainTask,
builds, and writes every build message to a JSON result file.

The runner substitutes __JOB_PATH__ before launching.
"""
from __future__ import print_function
import os, json, traceback
import scriptengine as se

JOB_PATH = r"__JOB_PATH__"

job = json.load(open(JOB_PATH))
result = {"ok": False, "created": [], "create_errors": [], "messages": [], "log": []}


def log(msg):
    result["log"].append(msg)
    print(msg)


def write_result():
    with open(job["result_path"], "w") as f:
        json.dump(result, f, indent=2)


def find_application(project):
    for child in project.get_children(True):
        if getattr(child, "is_application", False):
            return child
    raise RuntimeError("No Application object found in template project")


def find_main_task(project, app):
    for child in project.get_children(True):
        if child.get_name() == "MainTask" and hasattr(child, "pous"):
            return child
    task_config = None
    for child in app.get_children(False):
        if hasattr(child, "create_task"):
            task_config = child
    if task_config is None:
        task_config = app.create_task_configuration()
    return task_config.create_task("MainTask")


def find_device(name):
    for d in se.device_repository.get_all_devices():
        if d.device_info.name == name:
            return d.device_id
    raise RuntimeError("Device '%s' is not installed in this CODESYS" % name)


POU_TYPES = {
    "Program": se.PouType.Program,
    "FunctionBlock": se.PouType.FunctionBlock,
    "Function": se.PouType.Function,
}


def create_object(app, obj):
    kind, name = obj["kind"], obj["name"]
    if kind == "dut":
        o = app.create_dut(name)
        o.textual_declaration.replace(obj["text"])
    elif kind == "gvl":
        o = app.create_gvl(name)
        o.textual_declaration.replace(obj["text"])
    elif kind == "pou":
        ptype = POU_TYPES[obj["pou_type"]]
        if obj["pou_type"] == "Function":
            o = app.create_pou(name, ptype, None, obj.get("return_type") or "BOOL")
        else:
            o = app.create_pou(name, ptype)
        o.textual_declaration.replace(obj["decl"])
        if obj.get("impl") is not None and o.has_textual_implementation:
            o.textual_implementation.replace(obj["impl"])
    else:
        raise ValueError("Unknown object kind %r" % kind)
    return o


def message_to_dict(m):
    d = {}
    for attr in ("severity", "text", "prefix", "number", "position_text"):
        try:
            v = getattr(m, attr)
        except Exception:
            continue
        if v is not None:
            d[attr] = unicode(v)
    try:
        if m.object is not None:
            d["object"] = m.object.get_name()
    except Exception:
        pass
    return d


def find_device_object(project):
    for child in project.get_children(False):
        if hasattr(child, "set_simulation_mode"):
            return child
    raise RuntimeError("No device object supporting simulation mode")


def run_in_simulation(project, app, task, run):
    """Download to the CODESYS simulator, run until done_var is TRUE, read values."""
    import time
    out = {"values": {}, "timed_out": False}
    find_device_object(project).set_simulation_mode(True)
    onlineapp = se.online.create_online_application(app)
    try:
        onlineapp.login(se.OnlineChangeOption.Never, True)
        onlineapp.start()
        deadline = time.time() + run.get("timeout_s", 120)
        while True:
            done = unicode(onlineapp.read_value(run["done_var"]))
            if done.upper() == "TRUE":
                break
            if time.time() > deadline:
                out["timed_out"] = True
                break
            time.sleep(0.2)
        for var in run.get("read_vars", []):
            try:
                out["values"][var] = unicode(onlineapp.read_value(var))
            except Exception as e:
                out["values"][var] = "<read failed: %s>" % e
        try:
            onlineapp.stop()
        except Exception:
            pass
    finally:
        try:
            onlineapp.logout()
        except Exception:
            pass
    return out


try:
    target = job["project_path"]
    if os.path.exists(target):
        os.remove(target)
    project = se.projects.create(target, True)
    dev = find_device(job.get("device_name", "CODESYS Control Win V3 x64"))
    project.add("Device", dev.type, dev.id, dev.version)
    app = find_application(project)

    libman = [c for c in app.get_children(False) if hasattr(c, "add_library")]
    for lib in job.get("libraries", []):
        try:
            libman[0].add_library(lib)
        except Exception as e:
            result["create_errors"].append({"name": lib, "error": "add library: %s" % e})
    # Scripted add_library references default to qualified-only access, but
    # RTAC logic calls TON / R_TRIG / RS unqualified.
    if libman:
        for ref in libman[0].references:
            if getattr(ref, "qualified_only", False):
                try:
                    ref.qualified_only = False
                except Exception as e:
                    result["create_errors"].append({"name": unicode(ref.name), "error": "qualified_only: %s" % e})
        result["libraries"] = [unicode(r.name) for r in libman[0].references]

    for obj in job["objects"]:
        try:
            create_object(app, obj)
            result["created"].append(obj["name"])
        except Exception as e:
            result["create_errors"].append({"name": obj["name"], "error": unicode(e)})

    task = find_main_task(project, app)
    for name in job.get("task_programs", []):
        try:
            task.pous.add(name)
        except Exception as e:
            result["create_errors"].append({"name": name, "error": "task assign: %s" % e})

    cats = list(se.system.get_message_categories(True))
    for cat in cats:
        try:
            se.system.clear_messages(cat)
        except Exception:
            pass
    app.build()

    cats = list(se.system.get_message_categories(True))
    for cat in cats:
        try:
            desc = se.system.get_message_category_description(cat)
        except Exception:
            desc = unicode(cat)
        try:
            objs = se.system.get_message_objects(cat)
        except Exception as e:
            log("get_message_objects failed for %s: %s" % (desc, e))
            continue
        for m in objs:
            d = message_to_dict(m)
            d["category"] = desc
            result["messages"].append(d)

    run = job.get("run")
    build_errors = [m for m in result["messages"] if m.get("severity") == "Error" and m.get("category") == "Build"]
    if run and not build_errors and not result["create_errors"]:
        result["run"] = run_in_simulation(project, app, task, run)

    project.save()
    project.close()
    result["ok"] = True
except Exception:
    result["fatal"] = traceback.format_exc()
finally:
    write_result()
