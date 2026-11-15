"""Isolated hc/herdr-app fixture. No core, app, terminal or real agent is used.

Copied as both executables beside herdr-fixture.json. Records all argv/RPC calls;
only runs live.sh when the fixture explicitly enables run_script.
"""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent


def block(terminal):
    return {"type": "block", "id": "pane-" + terminal, "content": {"terminal": terminal}, "size": {"weight": 1.0}}


def main():
    with (HERE / "herdr-fixture.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = json.loads((HERE / "herdr-fixture.json").read_text())
        path = HERE / "herdr-state.json"
        state = json.loads(path.read_text()) if path.exists() else {
            "doc": {"spaces": [{"id": "space-main", "name": "Work", "tabs": [{"id": "tab-main", "root": {
                "id": "root", "type": "row", "children": [block("t-main"), block("t-other")]
            }}]}]}, "version": 1, "terminals": [], "calls": [], "conflicts": 0}
        argv = sys.argv[1:]
        app = Path(sys.argv[0]).name == "herdr-app"
        request = json.loads(sys.stdin.readline()) if argv[:1] == ["connect"] else None
        state["calls"].append({"app": app, "argv": argv, "rpc": request})
        code, reply = 0, {}
        if config.get("fail") == ("app" if app else argv[0]):
            code, reply = 1, {"error": "fixture failure"}
        elif app:
            if argv[0] == "ls":
                spaces = []
                for s in state["doc"]["spaces"]:
                    tabs = []
                    for t in s["tabs"]:
                        terms = [] if config.get("main_missing") else [{"terminal": "t-main", "pane": "pane-t-main"}]
                        tabs.append({"id": t["id"], "terminals": terms})
                    spaces.append({"id": s["id"], "name": s["name"], "tabs": tabs})
                reply = {"spaces": spaces}
            elif argv[:2] == ["do", "space.new"]:
                state["doc"]["spaces"].append({"id": "space-ck", "name": "CK", "tabs": []})
        elif argv[0] == "run":
            address = "t-run-" + str(len(state["terminals"]) + 1)
            labels = dict(v.split("=", 1) for i, v in enumerate(argv) if i and argv[i-1] == "--label")
            state["terminals"].append({"address": address, "labels": labels})
            for s in state["doc"]["spaces"]:
                for tab in s["tabs"]:
                    if tab["id"] == labels.get("herdr.tab"):
                        tab["root"]["children"].append(block(address))
            reply = {"address": address}
            if config.get("run_script"):
                command = argv[argv.index("--") + 1:]
                env = dict(os.environ, HC_TERMINAL_ID=address)
                with (HERE / "herdr-child.log").open("ab") as log:
                    subprocess.Popen(command, cwd=argv[argv.index("--cwd") + 1], env=env,
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True, close_fds=True)
        elif argv[0] == "connect":
            method = request["method"]
            if method == "doc.get":
                reply = {"result": {"value": state["doc"], "version": state["version"]}}
            elif config.get("conflict") and not state["conflicts"]:
                state["conflicts"] += 1
                state["version"] += 1
                state["doc"]["spaces"][0]["tabs"][0]["root"]["children"].append(block("t-concurrent"))
                reply = {"error": {"message": "version conflict"}}
            elif request["params"]["version"] != state["version"]:
                reply = {"error": {"message": "version conflict"}}
            else:
                state["doc"] = request["params"]["value"]
                state["version"] += 1
                reply = {"result": {"version": state["version"]}}
            reply.update(jsonrpc="2.0", id=request["id"])
        elif argv[0] == "ls":
            wanted = argv[argv.index("--label") + 1] if "--label" in argv else None
            reply = {"terminals": [t for t in state["terminals"] if not wanted or
                                   t["labels"].get(wanted.split("=", 1)[0]) == wanted.split("=", 1)[1]]}
        elif argv[0] == "read":
            reply = config.get("tail", "working on fixture")
        elif argv[0] == "label":
            target = argv[argv.index("--json") + 1]
            for t in state["terminals"]:
                if t["address"] == target:
                    t["labels"].update(v.split("=", 1) for v in argv[argv.index(target) + 1:])
        path.write_text(json.dumps(state))
        print(reply if isinstance(reply, str) else json.dumps(reply))
        return code


if __name__ == "__main__":
    sys.exit(main())
