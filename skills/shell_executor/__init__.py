"""
Shell Executor Skill — runs shell commands in a sandboxed container.

This skill is used by the Hands service to execute shell commands
submitted by the Brain's code_execute tool.
"""

import json
import os
import subprocess


def run(args: dict) -> dict:
    """Execute a shell command and return the output."""
    code = args.get("code", "")
    if not code.strip():
        return {"status": "ERROR", "error": "No command provided", "output": ""}

    result = {"status": "SUCCESS", "output": "", "error": ""}

    try:
        proc = subprocess.run(
            ["sh", "-c", code],
            capture_output=True,
            text=True,
            timeout=25,
        )

        result["output"] = proc.stdout
        if proc.stderr:
            result["output"] += f"\n[stderr]\n{proc.stderr}"

        if proc.returncode != 0:
            result["status"] = "ERROR"
            result["error"] = proc.stderr or f"Process exited with code {proc.returncode}"

    except subprocess.TimeoutExpired:
        result["status"] = "ERROR"
        result["error"] = "Execution timed out (25s limit)"
    except Exception as e:
        result["status"] = "ERROR"
        result["error"] = str(e)

    return result


# Entry point for sandbox execution
if __name__ == "__main__":
    args_json = os.environ.get("SKILL_ARGUMENTS", "{}")
    args = json.loads(args_json)
    result = run(args)
    print(json.dumps(result))
