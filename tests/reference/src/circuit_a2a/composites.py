"""COMPOSITE types and events: before validating, they are expanded into what the core already knows (switches and conditional wires).

  * type `two_way_switch`: a switch that chooses between two paths. It is written as a part {type:two_way_switch, position:up|down}
    and a special wire, which expands into TWO conditional wires with opposite conditions:
        join  {two_way_switch:x, to:D, from_up:A, from_down:B}  ->  D<-A when up(x) ; D<-B when down(x)
        split {two_way_switch:x, from:O, to_up:A, to_down:B}    ->  A<-O when up(x) ; B<-O when down(x)
  * type `plug`: a switch whose "up" position is "plugged in". It accepts position plugged|unplugged and the conditions
    plugged(x) / unplugged(x); the events plug / unplug are raise / lower of the plug.
None of this reaches Prolog as a new type: the core only sees part(x, switch) and connects(D, O, up(x)).
"""
import re

from circuit_a2a.core import Rejection, _check_keys, _check_name

EXT_TYPES = ("two_way_switch", "plug")
PLUG_POSITION = {"plugged": "up", "unplugged": "down"}
EXT_ACTIONS = {"plug": "raise", "unplug": "lower"}
RE_COND_EXT = re.compile(r"^(up|down|ok|plugged|unplugged)\(([a-z][a-z0-9_]{0,23})\)$")
COND_ALIAS = {"plugged": "up", "unplugged": "down"}


def _cond(c, path, user_type):
    """user condition (text or {type,part}) -> base condition; plugged/unplugged only on plugs"""
    if c is None:
        return None
    if isinstance(c, dict):
        _check_keys(c, {"type", "part"}, path)
        c = f"{c.get('type')}({c.get('part')})"
    m = RE_COND_EXT.match(c) if isinstance(c, str) else None
    if not m:
        return c           # the validator will reject it with its own message
    k, pz = m.groups()
    if k in COND_ALIAS:
        if user_type.get(pz) != "plug":
            raise Rejection("invalid_condition", f"{k}({pz}): only plugs are plugged or unplugged", path)
        return f"{COND_ALIAS[k]}({pz})"
    return c


def expand_definition(d):
    """user JSON -> (base JSON, info). It does not validate: validate_definition does that with the result."""
    parts, wires = d.get("parts"), d.get("wires", [])
    ut = {}                                   # user type for the composites
    if not isinstance(parts, list):
        return d, {"user_type": ut, "groups": []}
    new_parts = []
    for i, p in enumerate(parts):
        if isinstance(p, dict) and p.get("type") in EXT_TYPES:
            q = dict(p)
            n = p.get("name")
            if isinstance(n, str):
                ut[n] = p["type"]
            q["type"] = "switch"
            if p["type"] == "plug" and q.get("position") in PLUG_POSITION:
                q["position"] = PLUG_POSITION[q["position"]]
            new_parts.append(q)
        else:
            new_parts.append(p)
    base, groups = [], []
    if isinstance(wires, list):
        for i, c in enumerate(wires):
            path = f"wires[{i}]"
            if isinstance(c, dict) and "two_way_switch" in c:
                x = c["two_way_switch"]
                _check_name(x, path + ".two_way_switch")
                if ut.get(x) != "two_way_switch":
                    raise Rejection("invalid_wire", f"{x} is not a part of type two_way_switch", path + ".two_way_switch")
                if "to" in c and ("from_up" in c or "from_down" in c):
                    _check_keys(c, {"two_way_switch", "to", "from_up", "from_down"}, path)
                    if "from_up" not in c or "from_down" not in c:
                        raise Rejection("missing_data", "a joining two_way_switch needs from_up and from_down", path)
                    new = [{"to": c["to"], "from": c["from_up"], "when": f"up({x})"},
                           {"to": c["to"], "from": c["from_down"], "when": f"down({x})"}]
                elif "from" in c and ("to_up" in c or "to_down" in c):
                    _check_keys(c, {"two_way_switch", "from", "to_up", "to_down"}, path)
                    if "to_up" not in c or "to_down" not in c:
                        raise Rejection("missing_data", "a splitting two_way_switch needs to_up and to_down", path)
                    new = [{"to": c["to_up"], "from": c["from"], "when": f"up({x})"},
                           {"to": c["to_down"], "from": c["from"], "when": f"down({x})"}]
                else:
                    raise Rejection("invalid_format", "two_way_switch wire: use {to, from_up, from_down} or {from, to_up, to_down}", path)
                groups.append({"two_way_switch": x, "bases": [len(base), len(base) + 1], "user_index": i})
                base.extend(new)
            elif isinstance(c, dict):
                q = dict(c)
                if "when" in q:
                    q["when"] = _cond(q["when"], path + ".when", ut)
                base.append(q)
            else:
                base.append(c)
    else:
        base = wires
    out = dict(d)
    out["parts"] = new_parts
    if "wires" in d:
        out["wires"] = base
    return out, {"user_type": ut, "groups": groups}


def expand_events(events, user_type, base_type=None):
    """[{action,part}] -> (base list, actions as the user said them). plug/unplug only on plugs."""
    if not isinstance(events, list):
        return events, None
    base, user = [], []
    for i, e in enumerate(events):
        if isinstance(e, dict) and e.get("action") in EXT_ACTIONS:
            pz = e.get("part")
            if user_type.get(pz) != "plug":
                raise Rejection("invalid_action", f"{e['action']} {pz}: only plugs are plugged or unplugged", f"events[{i}]")
            base.append({**e, "action": EXT_ACTIONS[e["action"]]})
            user.append(e["action"])
        else:
            base.append(e)
            user.append(e.get("action") if isinstance(e, dict) else None)
    return base, user
