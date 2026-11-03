"""
SEL RTAC type stubs for compiling RTAC user logic in plain CODESYS.

These are APPROXIMATIONS of the SEL library types, written from how RTAC
projects use them (tag types in device XML, member access in user logic).
They exist so the CODESYS compiler can type-check user logic; they do not
reproduce SEL runtime behaviour. If a compile error points at a stub object
(see `source: "stub"` in compile results), extend the definition here.
"""

import re

QUALITY_TYPES = [
    ("validity_t", """TYPE validity_t :
(
	good := 0,
	invalid := 1,
	reserved := 2,
	questionable := 3
);
END_TYPE
"""),
    ("detailQual_t", """TYPE detailQual_t :
STRUCT
	overflow : BOOL;
	outOfRange : BOOL;
	badReference : BOOL;
	oscillatory : BOOL;
	failure : BOOL;
	oldData : BOOL;
	inconsistent : BOOL;
	inaccurate : BOOL;
END_STRUCT
END_TYPE
"""),
    ("quality_t", """TYPE quality_t :
STRUCT
	validity : validity_t;
	detailQual : detailQual_t;
	source : BOOL;
	test : BOOL;
	operatorBlocked : BOOL;
END_STRUCT
END_TYPE
"""),
    ("timeStampValue_t", """TYPE timeStampValue_t :
STRUCT
	dateTime : DATE_AND_TIME;
	nanoseconds : UDINT;
END_STRUCT
END_TYPE
"""),
    ("timeStamp_t", """TYPE timeStamp_t :
STRUCT
	value : timeStampValue_t;
	quality : BYTE;
END_STRUCT
END_TYPE
"""),
    # Every device gets an auto-generated "<device>_POU" instance in RTAC.
    ("T_SEL_DevicePOU", """TYPE T_SEL_DevicePOU :
STRUCT
	EN : BOOL;
	Offline : BOOL;
END_STRUCT
END_TYPE
"""),
    ("colorRG_t", """TYPE colorRG_t :
(
	red := 0,
	green := 1
);
END_TYPE
"""),
]


def _cdc(name, members):
    body = "".join("\t%s;\n" % m for m in members)
    return (name, "TYPE %s :\nSTRUCT\n%s\tq : quality_t;\n\tt : timeStamp_t;\nEND_STRUCT\nEND_TYPE\n" % (name, body))


# Common data classes, keyed by the "Tag Type" values found in RTAC tag lists.
CDC_TYPES = [
    _cdc("SPS", ["stVal : BOOL"]),
    _cdc("DPS", ["stVal : BYTE"]),
    _cdc("INS", ["stVal : DINT"]),
    _cdc("UNS", ["stVal : UDINT"]),
    _cdc("MV", ["instMag : REAL", "mag : REAL"]),
    _cdc("CMV", ["instCVal_mag : REAL", "instCVal_ang : REAL"]),
    _cdc("BCR", ["actVal : LINT", "frVal : LINT"]),
    _cdc("STR", ["strVal : STRING(255)"]),
    _cdc("operSPC", ["ctlVal : BOOL"]),
    _cdc("operDPC", ["ctlVal : BOOL"]),
    _cdc("operAPC", ["setMag : REAL", "trigger : BOOL"]),
    ("SPC", """TYPE SPC :
STRUCT
	status : SPS;
	operPulse : operSPC;
	operLatchOn : operSPC;
	operLatchOff : operSPC;
END_STRUCT
END_TYPE
"""),
    ("APC", """TYPE APC :
STRUCT
	status : MV;
	oper : operAPC;
END_STRUCT
END_TYPE
"""),
]

# Members a tag name may end with that belong to the tag's CDC rather than
# naming a container (e.g. "IID_CFG.SPS021.stVal" with Tag Type SPS).
CDC_ATTRIBUTES = {"stval", "instmag", "mag", "q", "t", "ctlval", "setmag", "strval", "actval", "trigger"}

IEC_ELEMENTARY = {"BOOL", "BYTE", "WORD", "DWORD", "LWORD", "SINT", "INT", "DINT", "LINT",
                  "USINT", "UINT", "UDINT", "ULINT", "REAL", "LREAL", "TIME", "STRING"}

KNOWN_TAG_TYPES = {name for name, _ in QUALITY_TYPES + CDC_TYPES} | IEC_ELEMENTARY

FUNCTIONS = [
    {"name": "Sys_Time", "pou_type": "Function", "return_type": "timeStamp_t",
     "decl": "FUNCTION Sys_Time : timeStamp_t\nVAR_INPUT\nEND_VAR\n",
     "impl": ""},
    {"name": "ValidityCheck", "pou_type": "Function", "return_type": "BOOL",
     "decl": "FUNCTION ValidityCheck : BOOL\nVAR_IN_OUT\n\ttq : quality_t;\nEND_VAR\n",
     "impl": "ValidityCheck := tq.validity = validity_t.good;\n"},
    {"name": "TPUDO", "pou_type": "FunctionBlock",
     "decl": "FUNCTION_BLOCK TPUDO\nVAR_INPUT\n\tIN : BOOL;\n\tPUP : TIME;\n\tDOUT : TIME;\nEND_VAR\n"
             "VAR_OUTPUT\n\tQ : BOOL;\nEND_VAR\nVAR\n\tton_ : TON;\n\ttof_ : TOF;\nEND_VAR\n",
     "impl": "ton_(IN := IN, PT := PUP);\ntof_(IN := ton_.Q, PT := DOUT);\nQ := tof_.Q;\n"},
    {"name": "TEX", "pou_type": "FunctionBlock",
     "decl": "FUNCTION_BLOCK TEX\nVAR_INPUT\n\tIN : BOOL;\n\tPT : TIME;\nEND_VAR\n"
             "VAR_OUTPUT\n\tQ : BOOL;\n\tET : TIME;\nEND_VAR\nVAR\n\tton_ : TON;\nEND_VAR\n",
     "impl": "ton_(IN := IN, PT := PT);\nQ := ton_.Q;\nET := ton_.ET;\n"},
]

SYSTEM_OBJECTS = [
    {"kind": "dut", "name": "T_System_time_control_pou",
     "text": "TYPE T_System_time_control_pou :\nSTRUCT\n\tSystem_Time : timeStamp_t;\nEND_STRUCT\nEND_TYPE\n"},
    {"kind": "gvl", "name": "GVL_SEL_System",
     "text": "VAR_GLOBAL\n\tSystem_time_control_pou : T_System_time_control_pou;\nEND_VAR\n"},
]


def library_objects():
    """Fixed SEL library stubs as compile-job objects."""
    objs = [{"kind": "dut", "name": n, "text": t} for n, t in QUALITY_TYPES + CDC_TYPES]
    objs += [dict(kind="pou", **f) for f in FUNCTIONS]
    objs += SYSTEM_OBJECTS
    return objs


def tag_objects(tags, devices=(), existing_globals=()):
    """
    Build DUTs + one GVL that declare every device / tag-list tag.

    `tags` maps full tag names ("PQMAP_DNP.FCA_ERROR.status") to tag types.
    Each top-level name becomes a global struct instance; intermediate path
    segments become generated struct types. `devices` are device object names
    (each gets a "<name>_POU" status instance). Names in `existing_globals`
    are already declared by user logic (e.g. Tag Processor destinations) and
    are skipped.
    """
    skip = {g.lower() for g in existing_globals}
    tree = {}
    for full_name, tag_type in sorted(tags.items()):
        if full_name.split(".")[0].lower() in skip:
            continue
        parts = full_name.split(".")
        if len(parts) > 2 and parts[-1].lower() in CDC_ATTRIBUTES:
            parts = parts[:-1]
        if len(parts) == 1:
            # Alias: a top-level global of the tag's type.
            if not isinstance(tree.get(parts[0]), dict):
                tree[parts[0]] = tag_type
            continue
        node = tree
        for seg in parts[:-1]:
            child = node.setdefault(seg, {})
            if isinstance(child, str):
                # Parent is already a typed tag (e.g. APC with .oper/.status
                # children): its stub type covers the children.
                node = None
                break
            node = child
        if node is None:
            continue
        leaf = parts[-1]
        if not isinstance(node.get(leaf), dict):
            node[leaf] = tag_type

    duts, globals_, used_names = [], [], set()

    def stub_type(tag_type):
        return tag_type if tag_type in KNOWN_TAG_TYPES else "SPS"

    def type_name_for(path):
        # CODESYS rejects consecutive underscores, so join with one and
        # de-duplicate the rare collisions that causes.
        base = re.sub(r"_+", "_", "T_" + "_".join(path)).rstrip("_")
        name, n = base, 2
        while name.lower() in used_names:
            name, n = "%s_%d" % (base, n), n + 1
        used_names.add(name.lower())
        return name

    def emit(path, node):
        type_name = type_name_for(path)
        members = []
        for name, child in sorted(node.items(), key=lambda kv: kv[0].lower()):
            if isinstance(child, dict):
                members.append("\t%s : %s;" % (name, emit(path + [name], child)))
            else:
                members.append("\t%s : %s;" % (name, stub_type(child)))
        duts.append({"kind": "dut", "name": type_name,
                     "text": "TYPE %s :\nSTRUCT\n%s\nEND_STRUCT\nEND_TYPE\n" % (type_name, "\n".join(members))})
        return type_name

    for top, node in sorted(tree.items()):
        if isinstance(node, dict):
            globals_.append("\t%s : %s;" % (top, emit([top], node)))
        else:
            globals_.append("\t%s : %s;" % (top, stub_type(node)))
    for dev in sorted(devices):
        if (dev + "_POU").lower() not in skip:
            globals_.append("\t%s_POU : T_SEL_DevicePOU;" % dev)

    unknown = sorted({t for t in tags.values() if t not in KNOWN_TAG_TYPES})
    gvl = {"kind": "gvl", "name": "GVL_SEL_Tags", "text": "VAR_GLOBAL\n%s\nEND_VAR\n" % "\n".join(globals_)}
    return duts + [gvl], unknown
