"""
SEL RTAC MCP Server
Export, read, edit and re-import AcSELerator RTAC user logic through
AcRtacCmd.exe, and type-check it by compiling in CODESYS against SEL stubs.
"""

import asyncio
import json
import logging
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from tools import acrtac, rtac_xml, codesys_compile, unit_tests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rtac-mcp")

app = Server("rtac-mcp-server")

EXPORT_DIR = {"type": "string", "description": "Path to an RTAC XML export folder (contains 'Project Info.xml')"}


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="rtac_status",
            description=(
                "Report whether the background AcSELerator RTAC process is running and logged in "
                "to the AcRTAC database. If not logged in, returns the PowerShell login command "
                "the user must run."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="rtac_list_projects",
            description="List projects in the AcRTAC database with RTAC type (e.g. 3530) and firmware version (e.g. R146).",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="rtac_export_project",
            description=(
                "Export an AcRTAC project to an XML folder (read-only for the project). "
                "User logic lands in <device>/User Logic as one XML file per POU/GVL/DataType. Takes ~30-60 s."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name exactly as listed"},
                    "output_dir": {"type": "string", "description": "Empty or new folder to export into"},
                },
                "required": ["project", "output_dir"],
            },
        ),
        Tool(
            name="rtac_import_project",
            description=(
                "Import an XML export folder into the AcRTAC database as a NEW project. The name must "
                "start with the scratch prefix (default 'MCP_'); existing projects are never overwritten. "
                "Takes ~45 s+."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "New project name, e.g. MCP_MyProject_edit1"},
                    "device_type": {"type": "string", "description": "RTAC type from rtac_list_projects, e.g. 3530"},
                    "version": {"type": "string", "description": "Firmware version from rtac_list_projects, e.g. R146"},
                    "export_dir": EXPORT_DIR,
                },
                "required": ["project", "device_type", "version", "export_dir"],
            },
        ),
        Tool(
            name="rtac_delete_project",
            description="Delete a scratch project from the AcRTAC database. Only names starting with the scratch prefix (default 'MCP_') are allowed.",
            inputSchema={
                "type": "object",
                "properties": {"project": {"type": "string"}},
                "required": ["project"],
            },
        ),
        Tool(
            name="list_logic_objects",
            description="List the POUs (Program/FunctionBlock/Function), GVLs and DataTypes in an export's User Logic, with folders.",
            inputSchema={"type": "object", "properties": {"export_dir": EXPORT_DIR}, "required": ["export_dir"]},
        ),
        Tool(
            name="get_logic_object",
            description=(
                "Return the Structured Text of one user-logic object. POUs return 'interface' (declaration) "
                "and 'implementation'; GVLs and DataTypes return 'content'."
            ),
            inputSchema={
                "type": "object",
                "properties": {"export_dir": EXPORT_DIR, "name": {"type": "string"}},
                "required": ["export_dir", "name"],
            },
        ),
        Tool(
            name="set_logic_object",
            description=(
                "Replace the code of an existing object in the export folder. For POUs pass 'interface' "
                "and/or 'implementation'; for GVLs/DataTypes pass 'content'. Only the given sections change."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "export_dir": EXPORT_DIR,
                    "name": {"type": "string"},
                    "interface": {"type": "string", "description": "Full POU declaration, e.g. 'FUNCTION_BLOCK X\\nVAR_INPUT ...'"},
                    "implementation": {"type": "string", "description": "POU body"},
                    "content": {"type": "string", "description": "Full GVL or DataType text"},
                },
                "required": ["export_dir", "name"],
            },
        ),
        Tool(
            name="create_logic_object",
            description="Create a new POU, GVL or DataType file in the export's User Logic (optionally in a subfolder).",
            inputSchema={
                "type": "object",
                "properties": {
                    "export_dir": EXPORT_DIR,
                    "name": {"type": "string"},
                    "kind": {"type": "string", "enum": ["POU", "GVL", "DataType"]},
                    "pou_kind": {"type": "string", "enum": ["Program", "FunctionBlock", "Function"]},
                    "interface": {"type": "string"},
                    "implementation": {"type": "string"},
                    "content": {"type": "string"},
                    "folder": {"type": "string", "description": "Subfolder under User Logic, e.g. 'Microgrid'"},
                },
                "required": ["export_dir", "name", "kind"],
            },
        ),
        Tool(
            name="list_tasks",
            description="List the RTAC tasks (MainTask and others) with cycle times and the programs they call.",
            inputSchema={"type": "object", "properties": {"export_dir": EXPORT_DIR}, "required": ["export_dir"]},
        ),
        Tool(
            name="assign_program_to_task",
            description=(
                "Append a Program to a task's explicit call list in Main Controller.xml. Tasks that use "
                "RTAC's default execution order (no list) are left unchanged, because adding a list "
                "would push devices and the Tag Processor to the end of the execution order."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "export_dir": EXPORT_DIR,
                    "program": {"type": "string"},
                    "task": {"type": "string", "description": "Task name; defaults to MainTask"},
                },
                "required": ["export_dir", "program"],
            },
        ),
        Tool(
            name="list_device_tags",
            description="List tags declared by devices, tag lists and system objects (name -> SEL tag type), optionally filtered by prefix.",
            inputSchema={
                "type": "object",
                "properties": {"export_dir": EXPORT_DIR, "prefix": {"type": "string", "description": "e.g. 'Greensmith_DNP.'"}},
                "required": ["export_dir"],
            },
        ),
        Tool(
            name="compile_logic",
            description=(
                "Type-check all user logic in an export by compiling it headless in CODESYS against "
                "SEL type stubs and generated device-tag globals. Returns errors with object, section, "
                "line and column matching the RTAC source; 'source: stub' errors indicate stub gaps "
                "rather than logic bugs. Use this instead of AcRtacCmd check, which hides compile errors. "
                "Takes ~30-90 s."
            ),
            inputSchema={"type": "object", "properties": {"export_dir": EXPORT_DIR}, "required": ["export_dir"]},
        ),
        Tool(
            name="run_unit_tests",
            description=(
                "Unit-test one POU (FunctionBlock, Program or Function) from an export by running it in "
                "the CODESYS simulator. Each case is a list of steps; a step sets inputs, runs the target "
                "for `cycles` and/or `hold_ms` of real time, then checks `expect` values. Returns pass/fail "
                "per check with actual values. Names declared by the target (e.g. 'Enable', "
                "'IID_com_delay.Q') refer to the target instance; other names are globals or device tags "
                "written as-is (e.g. 'Greensmith_DNP.ISLAND_ENABLED.stVal'). For Functions, use 'return' "
                "or 'return.<field>'. Values: true/false, numbers (compared within tolerance), or strings "
                "holding raw Structured Text ('T#5S', 'dt_State.Idle', \"'text'\"). Each FunctionBlock "
                "case gets a fresh instance; Program state carries over between cases. Only the target "
                "runs (other programs don't), so set anything they would provide as inputs. Simulator "
                "cycles are ~20 ms: use hold_ms for timer tests. Pass `suite_file` instead of "
                "target/cases to run suites saved as JSON. `each_cycle` (also a suite-file key) is raw "
                "ST run every cycle before the target, for FB calls the target relies on but another "
                "program normally makes (e.g. global timers). Takes ~45-90 s per suite."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "export_dir": EXPORT_DIR,
                    "target": {"type": "string", "description": "POU to test"},
                    "cases": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "steps": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "inputs": {"type": "object"},
                                            "cycles": {"type": "integer", "minimum": 1},
                                            "hold_ms": {"type": "number", "minimum": 0},
                                            "expect": {"type": "object"},
                                        },
                                    },
                                },
                            },
                            "required": ["steps"],
                        },
                    },
                    "tolerance": {"type": "number", "description": "Numeric comparison tolerance (default 1e-4)"},
                    "suite_file": {"type": "string", "description": "JSON file with {target, cases, each_cycle?} or a list of them"},
                    "each_cycle": {"type": "array", "items": {"type": "string"},
                                   "description": "ST statements run every cycle before the target, e.g. "
                                                  "'gv_UtilitySourceOK_10S(IN := gv_UtilitySourceOK, PT := T#10S)'"},
                },
                "required": ["export_dir"],
            },
        ),
    ]


def _dispatch(name, a):
    if name == "rtac_status":
        return acrtac.status()
    if name == "rtac_list_projects":
        return acrtac.list_projects()
    if name == "rtac_export_project":
        return acrtac.export_project(a["project"], a["output_dir"])
    if name == "rtac_import_project":
        return acrtac.import_project(a["project"], a["device_type"], a["version"], a["export_dir"])
    if name == "rtac_delete_project":
        return acrtac.delete_project(a["project"])
    if name == "list_logic_objects":
        return [rtac_xml.summary(o) for o in rtac_xml.list_objects(a["export_dir"])]
    if name == "get_logic_object":
        o = rtac_xml.find_object(a["export_dir"], a["name"])
        return {k: v for k, v in o.items() if k != "path"}
    if name == "set_logic_object":
        return rtac_xml.set_object(a["export_dir"], a["name"], a.get("interface"),
                                   a.get("implementation"), a.get("content"))
    if name == "create_logic_object":
        return rtac_xml.create_object(a["export_dir"], a["name"], a["kind"], a.get("pou_kind"),
                                      a.get("interface", ""), a.get("implementation", ""),
                                      a.get("content", ""), a.get("folder", ""))
    if name == "list_tasks":
        return rtac_xml.list_tasks(a["export_dir"])
    if name == "assign_program_to_task":
        return rtac_xml.assign_program_to_task(a["export_dir"], a["program"], a.get("task", "MainTask"))
    if name == "list_device_tags":
        prefix = a.get("prefix", "")
        return {k: v for k, v in sorted(rtac_xml.list_tags(a["export_dir"]).items()) if k.startswith(prefix)}
    if name == "compile_logic":
        return codesys_compile.compile_export(a["export_dir"])
    if name == "run_unit_tests":
        if a.get("suite_file"):
            return unit_tests.run_suite_file(a["export_dir"], a["suite_file"])
        if not a.get("target") or not a.get("cases"):
            raise ValueError("Pass either suite_file, or target and cases")
        return unit_tests.run_unit_tests(a["export_dir"], a["target"], a["cases"], a.get("tolerance", 1e-4),
                                         each_cycle=a.get("each_cycle"))
    return {"error": f"Unknown tool: {name}"}


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        # AcRtacCmd and CODESYS calls block for tens of seconds; keep the event loop free.
        result = await asyncio.to_thread(_dispatch, name, arguments)
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    except Exception as e:
        logger.exception(f"Tool {name} failed")
        return [TextContent(type="text", text=json.dumps({"error": str(e)}, indent=2))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
