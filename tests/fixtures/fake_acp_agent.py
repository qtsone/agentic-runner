"""A fake ACP agent: speaks the bridge side of the protocol from a scenario file.

Spawned by tests/unit/test_acp_runtime.py in place of a pinned bridge. It records its argv,
the environment it was started with and every message it receives into ``record``, asks
for each permission the scenario lists during ``session/prompt``, and answers with the
scenario's stop reason, usage or error.
"""

import json
import os
import sys

with open(os.environ["FAKE_ACP_SCENARIO"]) as scenario_file:
    scenario = json.load(scenario_file)
record_path = scenario["record"]
next_id = 1000


def record(entry: dict) -> None:
    with open(record_path, "a") as handle:
        handle.write(json.dumps(entry) + "\n")


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def read() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    message = json.loads(line)
    record({"received": message})
    return message


def reply(request: dict, result: dict | None = None, error: dict | None = None) -> None:
    message = {"jsonrpc": "2.0", "id": request["id"]}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result or {}
    send(message)


record({"argv": sys.argv[1:], "env": dict(os.environ)})
if "probe_reads" in scenario:
    # What the bridge and the harness it would spawn can see, as the uid they run as.
    import subprocess

    readable = {}
    for path in scenario["probe_reads"]:
        try:
            with open(path) as probed:
                readable[path] = probed.read()
        except OSError as error:
            readable[path] = type(error).__name__
    child = subprocess.run(["id", "-u"], capture_output=True, text=True, check=False)
    record({"uid": os.getuid(), "child_uid": child.stdout.strip(), "reads": readable})
codex_home = os.environ.get("CODEX_HOME")
if codex_home and os.path.isdir(codex_home):
    config_path = os.path.join(codex_home, "config.toml")
    config = None
    if os.path.exists(config_path):
        with open(config_path) as config_file:
            config = config_file.read()
    record({"codex_home_files": sorted(os.listdir(codex_home)), "codex_config": config})
while (message := read()) is not None:
    method = message.get("method")
    if method == "initialize":
        reply(message, {"protocolVersion": 1, "agentCapabilities": {}, "authMethods": []})
    elif method == "session/new":
        if "new_error" in scenario:
            reply(message, error=scenario["new_error"])
            continue
        reply(
            message,
            {
                "sessionId": "session-1",
                "modes": {
                    "currentModeId": "default",
                    "availableModes": [{"id": "default"}, {"id": "acceptEdits"}],
                },
            },
        )
    elif method == "session/prompt" and scenario.get("hang"):
        continue
    elif method == "session/prompt":
        for tool_call in scenario.get("permissions", []):
            next_id += 1
            send(
                {
                    "jsonrpc": "2.0",
                    "id": next_id,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": "session-1",
                        "toolCall": tool_call,
                        "options": [
                            {"optionId": "approved", "name": "Yes", "kind": "allow_once"},
                            {
                                "optionId": "approved-always",
                                "name": "Always",
                                "kind": "allow_always",
                            },
                            {"optionId": "cancel", "name": "No", "kind": "reject_once"},
                        ],
                    },
                }
            )
            while (answer := read()) is not None and answer.get("id") != next_id:
                pass
        send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "session-1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": scenario.get("text", "done")},
                    },
                },
            }
        )
        if "prompt_error" in scenario:
            reply(message, error=scenario["prompt_error"])
            continue
        reply(
            message,
            {
                "stopReason": scenario.get("stop_reason", "end_turn"),
                "_meta": {"quota": {"model_usage": scenario.get("model_usage", [])}},
            },
        )
    elif "id" in message:
        reply(message, {})
