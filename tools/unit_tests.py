"""
Unit-test RTAC user logic by running it in the CODESYS simulator.

A test suite targets one POU from the export (FunctionBlock, Program or
Function). Each case is a list of steps; each step sets inputs, calls the
target for a number of task cycles, then checks expected values. All of it is
generated into one runner program that executes inside the PLC and records
pass/fail plus the actual value of every check; the script only reads the
results once the runner reports done.

Value conventions (inputs and expectations):
  * JSON true/false  -> BOOL
  * JSON numbers     -> numeric; expectations compare within `tolerance`
  * JSON strings     -> raw Structured Text, e.g. "T#5S",
                        "dt_CriticalState.SysNorm_0", "'text'" for a STRING

Name conventions:
  * A name whose first segment is declared in the target's interface (e.g.
    "Enable", "IID_com_delay.Q") refers to that member of the target instance.
  * Anything else is a global expression written as-is
    (e.g. "gv_SysIslanded", "Greensmith_DNP.ISLAND_ENABLED.stVal").
  * For Functions, inputs are call arguments and the result is "return"
    ("return.stVal" for a struct result).

Each FunctionBlock case gets a fresh instance. Programs are single instances,
so their state carries over from one case to the next. Only the runner is
scheduled: other programs don't run, so they can't overwrite test inputs.
Logic they would normally provide (e.g. values another program writes into a
global) must be set as inputs.
"""

import math
import re

from . import codesys_compile, rtac_xml

RUNNER = "RtacMcp_TestRunner"
# Observed cycle time of the CODESYS simulator (the configured 10 ms task runs
# at ~20 ms); only used to size the timeout.
SIM_CYCLE_MS = 25
_SECTION = re.compile(r"^\s*(VAR_INPUT|VAR_OUTPUT|VAR_IN_OUT|VAR_GLOBAL|VAR_TEMP|VAR_STAT|VAR)\b", re.I)
_DECL = re.compile(r"^\s*([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*(?:AT\s+\S+\s*)?:", re.M)


def _members(interface):
    """{lower name: (name, section)} for variables declared in a POU interface."""
    text = re.sub(r"\(\*.*?\*\)", "", interface, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    section, out = None, {}
    for line in text.splitlines():
        m = _SECTION.match(line)
        if m:
            section = m.group(1).upper()
            continue
        if re.match(r"^\s*END_VAR", line, re.I):
            section = None
            continue
        if section:
            d = _DECL.match(line)
            if d:
                for name in d.group(1).split(","):
                    name = name.strip()
                    out[name.lower()] = (name, section)
    return out


def _literal(value):
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("NaN/inf are not valid test values")
        text = repr(value)
        return text if any(c in text for c in ".eE") else text + ".0"
    if isinstance(value, str):
        return value
    raise ValueError("Unsupported test value %r (use bool, number or a Structured Text string)" % (value,))


class _Target:
    def __init__(self, obj):
        self.name = obj["name"]
        self.kind = obj["pou_kind"]
        self.members = _members(obj["interface"])
        if self.kind == "Function":
            m = codesys_compile._FUNC_RETURN.search(obj["interface"])
            self.return_type = m.group(1) if m else None

    def ref(self, key, instance):
        """ST expression for an input/expectation key."""
        head, _, rest = key.partition(".")
        if self.kind == "Function":
            if head.lower() in ("return", self.name.lower()):
                return "_ret" + ("." + rest if rest else "")
            return key
        member = self.members.get(head.lower())
        if member:
            return "%s.%s%s" % (instance, member[0], "." + rest if rest else "")
        return key


def build_runner(target, cases, tolerance):
    """Generate the runner program. Returns (pou object, checks metadata)."""
    if not cases:
        raise ValueError("No test cases given")
    decls, body, checks = [], [], []
    step_no = 0
    for ci, case in enumerate(cases, 1):
        name = case.get("name") or "case %d" % ci
        steps = case.get("steps")
        if not steps:
            raise ValueError("Case '%s' has no steps" % name)
        if target.kind == "FunctionBlock":
            instance = "_uut%d" % ci
            decls.append("\t%s : %s;" % (instance, target.name))
        else:
            instance = target.name
        for si, step in enumerate(steps, 1):
            lines = ["\t%d: // %s, step %d" % (step_no, name.replace("*)", ""), si)]
            inputs = step.get("inputs", {})
            if target.kind == "Function":
                for key in inputs:
                    if key.lower() not in target.members:
                        lines.append("\t\t%s := %s;" % (key, _literal(inputs[key])))
                args = ", ".join("%s := %s" % (target.members[k.lower()][0], _literal(v))
                                 for k, v in inputs.items() if k.lower() in target.members)
                call = "\t\t_ret := %s(%s);" % (target.name, args)
            else:
                for key, value in inputs.items():
                    member = target.members.get(key.partition(".")[0].lower())
                    if member and member[1] == "VAR_IN_OUT":
                        raise ValueError("'%s' is VAR_IN_OUT; pass a global instead" % key)
                    lines.append("\t\t%s := %s;" % (target.ref(key, instance), _literal(value)))
                call = "\t\t%s();" % instance
            cycles = max(int(step.get("cycles") or 1), 1)
            hold_ms = int(math.ceil(float(step.get("hold_ms") or 0)))
            # Holds are measured with TIME(), not cycle counts: the simulator's
            # real cycle time differs from the configured task interval.
            cond = "_cyc >= %d" % cycles
            if hold_ms:
                lines.append("\t\tIF _cyc = 0 THEN _t0 := TIME(); END_IF")
                cond += " AND TIME() - _t0 >= T#%dMS" % hold_ms
            lines += [call, "\t\t_cyc := _cyc + 1;", "\t\tIF %s THEN" % cond]
            for key, expected in step.get("expect", {}).items():
                checks.append({"case": name, "step": si, "name": key, "expected": expected})
                n = len(checks)
                expr = target.ref(key, instance)
                if isinstance(expected, bool):
                    cond = "(%s) = %s" % (expr, _literal(expected))
                elif isinstance(expected, (int, float)):
                    cond = "ABS(TO_LREAL(%s) - (%s)) <= %r" % (expr, _literal(float(expected)), float(tolerance))
                else:
                    cond = "(%s) = (%s)" % (expr, _literal(expected))
                lines.append("\t\t\tpass[%d] := %s;" % (n, cond))
                lines.append("\t\t\tactual[%d] := TO_STRING(%s);" % (n, expr))
            lines += ["\t\t\t_cyc := 0;", "\t\t\t_step := %d;" % (step_no + 1), "\t\tEND_IF"]
            body.append("\n".join(lines))
            step_no += 1
    if not checks:
        raise ValueError("No 'expect' entries in any step: nothing to check")
    if target.kind == "Function":
        if not target.return_type:
            raise ValueError("Could not determine return type of %s" % target.name)
        decls.append("\t_ret : %s;" % target.return_type)
    n = len(checks)
    decl = ("PROGRAM %s\nVAR\n\t_step : DINT;\n\t_cyc : DINT;\n\t_t0 : TIME;\n\tdone : BOOL;\n"
            "\tpass : ARRAY[1..%d] OF BOOL;\n\tactual : ARRAY[1..%d] OF STRING(80);\n%s\nEND_VAR\n"
            % (RUNNER, n, n, "\n".join(decls)))
    impl = "CASE _step OF\n%s\nELSE\n\tdone := TRUE;\nEND_CASE\n" % "\n".join(body)
    return {"kind": "pou", "name": RUNNER, "pou_type": "Program", "decl": decl, "impl": impl}, checks


def _clean(value):
    """'REAL#80' -> '80', "STRING#'abc'" -> 'abc'."""
    v = value
    if "#" in v and re.match(r"^[A-Z_]+#", v):
        v = v.split("#", 1)[1]
    if len(v) >= 2 and v[0] == v[-1] == "'":
        v = v[1:-1]
    return v


def run_suite_file(export_dir, suite_file):
    """
    Run suites saved as JSON: either one {"target", "cases", "tolerance"?}
    object or a list of them. Each suite is a separate simulator run.
    """
    import json
    with open(suite_file, encoding="utf-8") as f:
        suites = json.load(f)
    if isinstance(suites, dict):
        suites = [suites]
    results = [run_unit_tests(export_dir, s["target"], s["cases"], s.get("tolerance", 1e-4))
               for s in suites]
    return {
        "ok": all(r.get("ok") for r in results),
        "passed": sum(r.get("passed", 0) for r in results),
        "failed": sum(r.get("failed", 0) for r in results),
        "suites": results,
    }


def run_unit_tests(export_dir, target, cases, tolerance=1e-4, timeout_s=None):
    obj = rtac_xml.find_object(export_dir, target)
    if obj["kind"] != "POU":
        raise ValueError("'%s' is a %s; tests target a FunctionBlock, Program or Function" % (target, obj["kind"]))
    t = _Target(obj)
    runner, checks = build_runner(t, cases, tolerance)

    total_ms = sum(int(x) * SIM_CYCLE_MS for x in re.findall(r"_cyc >= (\d+)", runner["impl"]))
    total_ms += sum(int(x) for x in re.findall(r"T#(\d+)MS THEN", runner["impl"]))
    if timeout_s is None:
        timeout_s = max(60, int(total_ms / 1000.0 * 2) + 30)
    read_vars = ["%s.pass[%d]" % (RUNNER, i) for i in range(1, len(checks) + 1)]
    read_vars += ["%s.actual[%d]" % (RUNNER, i) for i in range(1, len(checks) + 1)]
    run = {"done_var": "%s.done" % RUNNER, "read_vars": read_vars, "timeout_s": timeout_s}

    compile_result, raw = codesys_compile.run_export(
        export_dir, extra_objects=[runner], run=run, timeout=timeout_s + 600,
        only_programs=[RUNNER])
    if not compile_result.get("ok"):
        compile_result["note"] = ("Tests did not run: the logic plus generated runner failed to compile. "
                                  "'source: test' errors are in the test definition (bad name or value).")
        compile_result["runner_code"] = runner["decl"] + runner["impl"]
        return compile_result
    sim = raw.get("run")
    if not sim:
        return {"ok": False, "error": "Simulation did not run", "work_dir": compile_result["work_dir"],
                "log": raw.get("log")}

    values = sim["values"]
    results, passed = [], 0
    for i, chk in enumerate(checks, 1):
        ok = values.get("%s.pass[%d]" % (RUNNER, i), "").upper() == "TRUE"
        passed += ok
        results.append({**chk, "pass": ok, "actual": _clean(values.get("%s.actual[%d]" % (RUNNER, i), ""))})

    cases_out = []
    for case in dict.fromkeys(c["case"] for c in checks):
        rows = [r for r in results if r["case"] == case]
        cases_out.append({"case": case, "pass": all(r["pass"] for r in rows),
                          "checks": rows if not all(r["pass"] for r in rows) else len(rows)})
    out = {
        "ok": passed == len(checks) and not sim.get("timed_out"),
        "target": t.name,
        "passed": passed,
        "failed": len(checks) - passed,
        "cases": cases_out,
        "warnings": [i for i in compile_result["issues"] if i.get("source") == "user"],
        "work_dir": compile_result["work_dir"],
        "note": ("Ran in the CODESYS simulator against approximate SEL stubs. Simulator cycles "
                 "are ~20 ms, so use hold_ms (real time) rather than cycles for timer tests and "
                 "keep a margin around timer presets. Device tags are plain variables here, so "
                 "set them as inputs; nothing comes from real I/O."),
    }
    if sim.get("timed_out"):
        out["error"] = "Runner did not finish within %d s; results may be incomplete" % timeout_s
    return out
