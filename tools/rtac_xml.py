"""
Read and edit an AcSELerator RTAC XML export folder (from `AcRtacCmd exportxml`).

Layout of an export:
    Project Info.xml            -> names the device folder (e.g. SEL_RTAC)
    <device>/User Logic/...     -> one XML file per POU / GVL / DataType
    <device>/System/Main Controller.xml  -> task configuration
    <device>/Devices, Virtual Tag Lists, System -> tag lists (Tag Name / Tag Type rows)

Writes are done as text edits on the CDATA sections so the rest of each file
is left byte-for-byte as AcSELerator wrote it.
"""

import os
import re
import xml.etree.ElementTree as ET

OBJECT_KINDS = ("POU", "GVL", "DataType")
POU_KINDS = ("Program", "FunctionBlock", "Function")
BOM = "﻿"


def _read(path):
    with open(path, encoding="utf-8-sig") as f:
        return f.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(BOM + text)


def device_folder(export_dir):
    info = os.path.join(export_dir, "Project Info.xml")
    if not os.path.exists(info):
        raise FileNotFoundError("Not an RTAC XML export (no 'Project Info.xml'): %s" % export_dir)
    root = ET.fromstring(_read(info))
    folder = root.findtext(".//Devices/Device/FolderName") or "SEL_RTAC"
    path = os.path.join(export_dir, folder)
    if not os.path.isdir(path):
        raise FileNotFoundError("Device folder '%s' missing from export" % folder)
    return path


def export_source(export_dir):
    """Schema / DeviceMOT to stamp on newly created objects."""
    root = ET.fromstring(_read(os.path.join(export_dir, "Project Info.xml")))
    dev = root.find(".//Devices/Device")
    return dev.findtext("Schema"), dev.findtext("DeviceMOT")


def user_logic_dir(export_dir):
    return os.path.join(device_folder(export_dir), "User Logic")


def _parse_object(path):
    root = ET.fromstring(_read(path))
    for kind in OBJECT_KINDS:
        el = root.find(kind)
        if el is not None:
            obj = {"name": el.findtext("Name"), "kind": kind, "path": path}
            if kind == "POU":
                obj["pou_kind"] = el.findtext("POUKind")
                obj["interface"] = el.findtext("Content/Interface") or ""
                obj["implementation"] = el.findtext("Content/Implementation") or ""
            else:
                obj["content"] = el.findtext("Content") or ""
            return obj
    return None


def list_objects(export_dir):
    ul = user_logic_dir(export_dir)
    out = []
    for d, _, files in os.walk(ul):
        for f in sorted(files):
            if not f.lower().endswith(".xml"):
                continue
            obj = _parse_object(os.path.join(d, f))
            if obj:
                obj["folder"] = os.path.relpath(d, ul).replace("\\", "/")
                if obj["folder"] == ".":
                    obj["folder"] = ""
                out.append(obj)
    return out


def find_object(export_dir, name):
    for obj in list_objects(export_dir):
        if obj["name"].lower() == name.lower():
            return obj
    raise KeyError("No POU/GVL/DataType named '%s' in %s" % (name, export_dir))


def summary(obj):
    s = {k: obj[k] for k in ("name", "kind", "folder") if k in obj}
    if obj.get("pou_kind"):
        s["pou_kind"] = obj["pou_kind"]
    return s


def _check_cdata(text, field):
    if "]]>" in text:
        raise ValueError("%s contains ']]>' which cannot be stored in RTAC XML" % field)


def _replace_cdata(xml_text, tag, new_value):
    pattern = re.compile(r"(<%s><!\[CDATA\[)(.*?)(\]\]></%s>)" % (tag, tag), re.S)
    if pattern.search(xml_text):
        return pattern.sub(lambda m: m.group(1) + new_value + m.group(3), xml_text, count=1)
    empty = re.compile(r"<%s\s*/>|<%s></%s>" % (tag, tag, tag))
    if empty.search(xml_text):
        return empty.sub("<%s><![CDATA[%s]]></%s>" % (tag, new_value, tag), xml_text, count=1)
    raise ValueError("No <%s> element to update" % tag)


def set_object(export_dir, name, interface=None, implementation=None, content=None):
    obj = find_object(export_dir, name)
    text = _read(obj["path"])
    if obj["kind"] == "POU":
        if content is not None:
            raise ValueError("'%s' is a POU: pass interface/implementation, not content" % name)
        if interface is not None:
            _check_cdata(interface, "interface")
            text = _replace_cdata(text, "Interface", interface)
        if implementation is not None:
            _check_cdata(implementation, "implementation")
            text = _replace_cdata(text, "Implementation", implementation)
    else:
        if interface is not None or implementation is not None:
            raise ValueError("'%s' is a %s: pass content only" % (name, obj["kind"]))
        if content is not None:
            _check_cdata(content, "content")
            text = _replace_cdata(text, "Content", content)
    _write(obj["path"], text)
    return summary(find_object(export_dir, name))


_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def create_object(export_dir, name, kind, pou_kind=None, interface="", implementation="",
                  content="", folder=""):
    if not _SAFE_NAME.match(name):
        raise ValueError("'%s' is not a valid IEC 61131-3 identifier" % name)
    if kind not in OBJECT_KINDS:
        raise ValueError("kind must be one of %s" % (OBJECT_KINDS,))
    try:
        find_object(export_dir, name)
        raise ValueError("An object named '%s' already exists" % name)
    except KeyError:
        pass
    schema, mot = export_source(export_dir)
    src = ("    <ExportSource>\n      <Schema>%s</Schema>\n      <DeviceMOT>%s</DeviceMOT>\n"
           "    </ExportSource>\n" % (schema, mot))
    if kind == "POU":
        if pou_kind not in POU_KINDS:
            raise ValueError("pou_kind must be one of %s" % (POU_KINDS,))
        _check_cdata(interface, "interface")
        _check_cdata(implementation, "implementation")
        body = ("    <Name>%s</Name>\n    <POUKind>%s</POUKind>\n    <Content>\n"
                "      <Interface><![CDATA[%s]]></Interface>\n"
                "      <Implementation><![CDATA[%s]]></Implementation>\n    </Content>\n"
                % (name, pou_kind, interface, implementation))
    else:
        _check_cdata(content, "content")
        body = "    <Name>%s</Name>\n    <Content><![CDATA[%s]]></Content>\n" % (name, content)
    xml = ('<?xml version="1.0" encoding="utf-8"?>\n<RTACModule>\n  <%s>\n%s%s  </%s>\n</RTACModule>'
           % (kind, src, body, kind))
    target_dir = os.path.join(user_logic_dir(export_dir), *[p for p in folder.split("/") if p])
    os.makedirs(target_dir, exist_ok=True)
    _write(os.path.join(target_dir, name + ".xml"), xml)
    return summary(find_object(export_dir, name))


# ---------------------------------------------------------------- tasks

def _main_controller_path(export_dir):
    return os.path.join(device_folder(export_dir), "System", "Main Controller.xml")


def list_tasks(export_dir):
    root = ET.fromstring(_read(_main_controller_path(export_dir)))
    mc = root.find("MainController")
    tasks = []
    for el in mc:
        if el.tag not in ("MainTask", "Task"):
            continue
        tasks.append({
            "name": "MainTask" if el.tag == "MainTask" else el.findtext("Name"),
            "cycle_time_ms": el.findtext("CycleTime"),
            "pous": [{"name": i.findtext("Name"), "enabled": i.findtext("Enabled") == "true"}
                     for i in el.findall("POUs/Item")],
        })
    return tasks


def assign_program_to_task(export_dir, program, task="MainTask"):
    obj = find_object(export_dir, program)
    if obj.get("pou_kind") != "Program":
        raise ValueError("'%s' is a %s; only Programs can be assigned to tasks" % (program, obj.get("pou_kind") or obj["kind"]))
    for t in list_tasks(export_dir):
        if any(p["name"].lower() == program.lower() for p in t["pous"]):
            raise ValueError("'%s' is already assigned to task '%s'" % (program, t["name"]))

    path = _main_controller_path(export_dir)
    text = _read(path)
    if task == "MainTask":
        m = re.search(r"<MainTask>.*?</MainTask>", text, re.S)
    else:
        m = None
        for tm in re.finditer(r"<Task>.*?</Task>", text, re.S):
            if re.search(r"<Name>%s</Name>" % re.escape(task), tm.group(0)):
                m = tm
                break
    if not m:
        raise KeyError("Task '%s' not found in Main Controller.xml" % task)
    block = m.group(0)
    if "<POUs" not in block:
        # Writing a list here would switch the task to an explicit order that
        # contains only this program; on import RTAC then moves every device
        # and the Tag Processor to the END of the execution order.
        return {"task": task, "changed": False,
                "note": ("Task '%s' uses RTAC's default execution order (no explicit POU list), so it "
                         "was left unchanged. Programs appear to be scheduled automatically in that "
                         "mode; confirm in AcSELerator if the order matters." % task)}
    indent = re.search(r"\n(\s*)<CycleTime>", block).group(1)
    item = ("%s  <Item>\n%s    <Enabled>true</Enabled>\n%s    <Name>%s</Name>\n%s  </Item>\n"
            % (indent, indent, indent, obj["name"], indent))
    new_block = block.replace("%s</POUs>" % indent, "%s%s</POUs>" % (item, indent), 1)
    _write(path, text[:m.start()] + new_block + text[m.end():])
    return [t for t in list_tasks(export_dir) if t["name"] == task][0]


# ---------------------------------------------------------------- tags

def list_devices(export_dir):
    """Device object names (file names under <device>/Devices)."""
    root = os.path.join(device_folder(export_dir), "Devices")
    return sorted(os.path.splitext(f)[0] for _, _, files in os.walk(root)
                  for f in files if f.lower().endswith(".xml"))


_GLOBAL_DECL = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?:AT\s+\S+\s*)?:", re.M)


def declared_globals(export_dir):
    """Variable names declared in user-logic GVLs."""
    names = set()
    for o in list_objects(export_dir):
        if o["kind"] == "GVL":
            text = re.sub(r"\(\*.*?\*\)", "", o["content"], flags=re.S)
            text = re.sub(r"//[^\n]*", "", text)
            names.update(_GLOBAL_DECL.findall(text))
    return names


def list_tags(export_dir):
    """
    All tags declared by devices, tag lists, system objects and the Tag
    Processor: {name: type}. A tag's alias is also a global name for it.
    """
    tags = {}
    for d, _, files in os.walk(device_folder(export_dir)):
        if os.path.basename(d) == "User Logic" or "User Logic" in os.path.relpath(d, export_dir).split(os.sep):
            continue
        for f in files:
            if not f.lower().endswith(".xml"):
                continue
            try:
                root = ET.fromstring(_read(os.path.join(d, f)))
            except ET.ParseError:
                continue
            for row in root.iter("Row"):
                cols = {s.findtext("Column"): (s.findtext("Value") or "") for s in row.findall("Setting")}
                if cols.get("Tag Name") and cols.get("Tag Type"):
                    tags[cols["Tag Name"]] = cols["Tag Type"]
                    if cols.get("Tag Alias"):
                        tags[cols["Tag Alias"]] = cols["Tag Type"]
                elif cols.get("DestinationTagName") and cols.get("DTDataType"):
                    tags[cols["DestinationTagName"]] = cols["DTDataType"]
    return tags


_TP_CONTROL = re.compile(r"^\s*(IF\b|ELSIF\b|ELSE\b|END_IF\b|CASE\b|END_CASE\b|FOR\b|END_FOR\b|WHILE\b|END_WHILE\b)", re.I)


def check_tag_processor(export_dir):
    """
    Tag Processor rows that AcSELerator reports as Setting Errors (the CODESYS
    build doesn't see these): destinations written by more than one built row
    ("Duplicate Tag name", error) and control-flow rows (warning; AcSELerator
    reports "Code not generated for statement" when they wrap duplicates).
    Returns compile-style issues.
    """
    path = os.path.join(device_folder(export_dir), "Tag Processor.xml")
    if not os.path.exists(path):
        return []
    root = ET.fromstring(_read(path))
    issues, first = [], {}
    for row in root.iter("Row"):
        cols = {s.findtext("Column"): (s.findtext("Value") or "") for s in row.findall("Setting")}
        if cols.get("Build") != "True":
            continue
        dest, src, order = cols.get("DestinationTagName", ""), cols.get("SourceExpression", ""), cols.get("SolveOrder")
        base = {"severity": "error", "object": "Tag Processor", "source": "tag_processor", "solve_order": order}
        if not dest and _TP_CONTROL.match(src):
            # Not an error by itself: AcSELerator reported "Code not generated for
            # statement" on these only alongside duplicate destinations inside them.
            issues.append(dict(base, severity="warning", code="TP_CONTROL",
                               message="Control statement row '%s': verify the Tag Processor accepts it; "
                                       "destinations inside IF/ELSE branches count as duplicates" % src.strip()))
            continue
        if not dest or dest.startswith("//"):
            continue
        if dest in first:
            issues.append(dict(base, code="TP_DUPLICATE",
                               message="Duplicate Tag name '%s' (also written at solve order %s)" % (dest, first[dest])))
        else:
            first[dest] = order
    return issues
