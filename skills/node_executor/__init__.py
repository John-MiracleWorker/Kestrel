"""
Node.js Executor Skill — runs JavaScript code in a sandboxed container.

This skill is used by the Hands service to execute JavaScript code
submitted by the Brain's code_execute tool.
"""

import json
import os
import subprocess
import tempfile


def run(args: dict) -> dict:
    """Execute JavaScript code via Node.js and return the output."""
    code = args.get("code", "")
    if not code.strip():
        return {"status": "ERROR", "error": "No code provided", "output": ""}

    result = {"status": "SUCCESS", "output": "", "error": ""}

    try:
        # Write code to a temp file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False) as f:
            f.write(code)
            tmp_path = f.name

        try:
            proc = subprocess.run(
                ["node", tmp_path],
                capture_output=True,
                text=True,
                timeout=55,
            )

            result["output"] = proc.stdout
            if proc.stderr:
                result["output"] += f"\n[stderr]\n{proc.stderr}"

            if proc.returncode != 0:
                result["status"] = "ERROR"
                result["error"] = proc.stderr or f"Process exited with code {proc.returncode}"

        finally:
            os.unlink(tmp_path)

    except subprocess.TimeoutExpired:
        result["status"] = "ERROR"
        result["error"] = "Execution timed out (55s limit)"
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
