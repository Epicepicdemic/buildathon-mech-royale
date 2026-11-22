"""Write a squad-settings variant (mech/squad_settings.json) from key=value arguments and commit it.

    python3 tools/variant.py lose_hp_two=0.4 assault_ratio=1.2 [--name aggro1] [--reset] [--no-commit]

Values are JSON (numbers) or, for upgrade_order, a comma list (upgrade_order=armor,plating).
Keys and types are checked against mech/squad_settings.py DEFAULTS. Without --reset the new values
merge into the current file; key=default removes that override. The commit touches only
mech/squad_settings.json, so a variant is a settings-only change of the build it sits on.
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from mech import squad_settings as SS      # noqa: E402


def parse(k, v):
    if isinstance(SS.DEFAULTS[k], list):
        return [x for x in v.split(",") if x] if v else []
    return json.loads(v)


def main(argv):
    name, reset, commit, pairs = None, False, True, []
    it = iter(argv)
    for a in it:
        if a == "--name":
            name = next(it)
        elif a == "--reset":
            reset = True
        elif a == "--no-commit":
            commit = False
        elif "=" in a:
            pairs.append(a.split("=", 1))
        else:
            sys.exit("not key=value: %s" % a)
    cur = {}
    if os.path.exists(SS.PATH) and not reset:
        with open(SS.PATH) as f:
            cur = json.load(f)
    for k, v in pairs:
        if k not in SS.DEFAULTS:
            sys.exit("unknown knob: %s (see mech/squad_settings.py)" % k)
        val = parse(k, v)
        if val == SS.DEFAULTS[k]:
            cur.pop(k, None)
        else:
            cur[k] = val
    tmp = SS.PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cur, f, indent=1, sort_keys=True)
        f.write("\n")
    SS.load(tmp)                             # refuse what the bot would refuse
    os.replace(tmp, SS.PATH)
    desc = " ".join("%s=%s" % (k, json.dumps(cur[k])) for k in sorted(cur)) or "defaults (B2)"
    print(desc)
    if commit:
        rel = os.path.relpath(SS.PATH, ROOT)
        subprocess.run(["git", "-C", ROOT, "add", rel], check=True)
        msg = "Squad variant %s: %s" % (name or "", desc)
        subprocess.run(["git", "-C", ROOT, "commit", "-q", "-m", msg, "--", rel], check=True)
        out = subprocess.run(["git", "-C", ROOT, "log", "--oneline", "-1"], capture_output=True, text=True)
        print(out.stdout.strip())


if __name__ == "__main__":
    main(sys.argv[1:])
