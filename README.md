# SEL RTAC MCP Server

An MCP server for AcSELerator RTAC user logic. It lets an LLM client export a project to XML, read and edit POUs, GVLs and DataTypes, re-import the result as a new project, and get real compile errors back.

## Why compiling goes through CODESYS

AcSELerator's own `AcRtacCmd check` returns `check:0:success` even when the logic has compile errors; the errors only appear in the GUI Output pane. So `compile_logic` copies the export's user logic **verbatim** into a fresh CODESYS project, adds generated SEL stubs, builds it headless, and returns each error with its object, section (interface/implementation), line and column. Because the text is copied unchanged, those positions match the RTAC source.

The stubs are generated from the export itself:

| Stub | Source |
|---|---|
| SEL types (`SPS`, `MV`, `INS`, `STR`, `operSPC`, `operAPC`, `APC`, `SPC`, quality/timestamp types) | Hand-written in `tools/sel_stubs.py` |
| SEL functions/FBs (`Sys_Time`, `ValidityCheck`, `TPUDO`, `TEX`) | Hand-written in `tools/sel_stubs.py` |
| Device tags (`Greensmith_DNP.ISLAND_ENABLED`, ...) | `Tag Name` / `Tag Type` rows in device, tag-list and system XML |
| Tag aliases (`SW1_52A3P`) | `Tag Alias` column |
| Tag Processor destinations (`Tags.X`) | `DestinationTagName` / `DTDataType` |
| Device status objects (`SCADA_DNP_POU.Offline`, `.EN`) | One per file under `Devices/` |

The stubs are approximations of the SEL library. An error tagged `"source": "stub"` means a stub needs extending, not that the logic is wrong. Behaviour is not simulated; this is a type check.

Every user FB is instantiated in a generated harness program, so new FBs are checked even before anything calls them. RTAC itself skips unreferenced POUs.

## Unit tests

`run_unit_tests` builds the export the same way as `compile_logic`, adds a generated runner program, downloads it to the CODESYS simulator and reads the results back. The runner steps through every case inside the PLC: set inputs, call the target for a number of cycles or a real-time hold, then compare the outputs against expected values.

```json
{
  "target": "FunctionBlock1",
  "cases": [
    {
      "name": "request over limit is clamped",
      "steps": [
        {
          "inputs": {"Requested_kW": 100.0, "MaxAllowed_kW": 80.0, "Enable": true, "CommsHealthy": true},
          "expect": {"Limited_kW": 80.0, "LimitActive": true}
        }
      ]
    }
  ]
}
```

- **Targets:** FunctionBlocks (a fresh instance per case), Programs (one instance, so state carries over between cases) and Functions (`return` / `return.<field>`).
- **Names:** a name whose first segment the target declares (`Enable`, `IID_com_delay.Q`) refers to the target instance. Anything else is a global or device tag, written as-is (`gv_SysIslanded`, `IID_CFG_MuGrdInputs_GRX_POU.Offline`).
- **Values:** `true`/`false`, numbers (compared within `tolerance`), or strings containing raw Structured Text (`"T#5S"`, `"validity_t.good"`, `"'text'"`).
- **Timing:** the simulator runs at about 20 ms per cycle, regardless of the configured task interval. Use `hold_ms` for timer behaviour, since it measures real time with `TIME()`, and leave a margin around timer presets.
- **Isolation:** only the runner is scheduled. Other programs don't run, so anything they would normally write (globals, device tags) has to be set as a test input.
- **Results:** pass/fail for every check, with the actual value. Errors in the test definition itself (a typo'd name, a wrong type) come back as compile errors with `"source": "test"`.

Suites can be saved as JSON (one `{target, cases}` object or a list of them) and run with `suite_file`; see [examples/FunctionBlock1.tests.json](examples/FunctionBlock1.tests.json).

## Tools

| Tool | What it does |
|---|---|
| `rtac_status` | Is the background RTAC process running and logged in? |
| `rtac_list_projects` | Projects in the AcRTAC database, with type and firmware version |
| `rtac_export_project` | Export a project to an XML folder (read-only for the project) |
| `rtac_import_project` | Import an XML folder as a **new** `MCP_*` project |
| `rtac_delete_project` | Delete an `MCP_*` scratch project |
| `list_logic_objects` / `get_logic_object` | Browse and read user logic |
| `set_logic_object` / `create_logic_object` | Edit or add POUs, GVLs and DataTypes in the export |
| `list_tasks` / `assign_program_to_task` | Task configuration (`System/Main Controller.xml`) |
| `list_device_tags` | Device and tag-list tags with SEL types |
| `compile_logic` | Type-check all user logic through CODESYS |
| `run_unit_tests` | Run test cases against one POU in the CODESYS simulator and report pass/fail |

The export/edit/compile tools work on a folder and don't need AcSELerator running. Only the `rtac_*` tools touch the AcRTAC database.

## Safety rails

- Imports only create new projects, and their names must start with `MCP_` (`RTAC_SCRATCH_PREFIX`).
- Deletes are refused for any project that doesn't start with that prefix.
- There are no device tools (connect, send, firmware); nothing in this server talks to an RTAC.

## Requirements

You only need the parts used by the tools you plan to run:

| Requirement | Needed for | Notes |
|---|---|---|
| Windows | everything | AcSELerator and CODESYS are Windows-only |
| Python 3.10+ | everything | the `mcp` package needs 3.10 or newer; tested on 3.15 |
| **AcSELerator RTAC** (with `AcRtacCmd.exe`) and access to an AcRTAC database (username and password) | `rtac_*` tools (list, export, import, delete) | Tested on 1.157. Not needed if you already have an exported XML folder |
| **CODESYS 3.5 SP22** (standalone IDE, from the CODESYS Store) with the `CODESYS Control Win V3 x64` device and the `Standard` library installed | `compile_logic`, `run_unit_tests` | Tested on SP22 Patch 4. This is separate from the CODESYS 3.5.10 embedded in AcSELerator, whose script engine can't be driven headless. Other versions should work if `CODESYS_PATH`, `CODESYS_PROFILE` and `CODESYS_STANDARD_LIBRARY` are set to match |

`run_unit_tests` uses the CODESYS simulator, so it doesn't need a soft-PLC runtime license.

## Setup

```powershell
cd C:\Dev\rtac-mcp-server
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Then register it with your MCP client as a stdio server. Most clients take a JSON entry like this:

```json
{
  "mcpServers": {
    "rtac": {
      "command": "C:\\Dev\\rtac-mcp-server\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Dev\\rtac-mcp-server\\server.py"]
    }
  }
}
```

### Logging in to the AcRTAC database

The `rtac_*` tools run against a background RTAC process (alias `rtacmcp`), which must be logged in. Either set `RTAC_DB_USER` / `RTAC_DB_PASSWORD` in the server's environment, or log it in yourself after the server has started it:

```powershell
& "C:\Program Files\SEL\AcSELerator\RTAC\AcRtacCmd.exe" login -a rtacmcp -p "<password>" <username>
```

### Environment variables

| Variable | Default |
|---|---|
| `ACRTACCMD_PATH` | `C:\Program Files\SEL\AcSELerator\RTAC\AcRtacCmd.exe` |
| `RTAC_ALIAS` | `rtacmcp` |
| `RTAC_SCRATCH_PREFIX` | `MCP_` |
| `RTAC_DB_USER` / `RTAC_DB_PASSWORD` | unset (log in by hand) |
| `CODESYS_PATH` | `C:\Program Files\CODESYS 3.5.22.40\CODESYS\Common\CODESYS.exe` |
| `CODESYS_PROFILE` | `CODESYS V3.5 SP22 Patch 4` |
| `CODESYS_DEVICE` | `CODESYS Control Win V3 x64` |
| `CODESYS_STANDARD_LIBRARY` | `Standard, 3.5.22.0 (System)` |
| `RTAC_MCP_WORK_DIR` | `%TEMP%\rtac-mcp` (each compile keeps its CODESYS project here) |

## Known limitations

- `importxml` can't update a project in place, so every round trip makes a new project.
- `assign_program_to_task` only appends to tasks that already have an explicit POU list. On a default-order task it changes nothing: a one-item list made RTAC move the devices and the Tag Processor to the end of the execution order on import. It appears that default-order tasks schedule programs automatically, but that hasn't been confirmed in the GUI.
- Stubs are approximate (see above). Functions that nothing calls are still not compiled.
- Tested against AcSELerator RTAC 1.157 (RTAC 3530 R146, 3350 R157) and CODESYS 3.5 SP22 Patch 4.

## License

MIT. See [LICENSE](LICENSE).
