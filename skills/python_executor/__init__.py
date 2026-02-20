"""
Python Executor Skill — runs Python code in a sandboxed container.

This skill is used by the Hands service to execute Python code
submitted by the Brain's code_execute tool.
"""

import io
import json
import sys
import traceback


def run(args: dict) -> dict:
    """Execute Python code and return the output."""
    code = args.get("code", "")
    if not code.strip():
        return {"status": "ERROR", "error": "No code provided", "output": ""}

    # Capture stdout and stderr
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdout = captured_out = io.StringIO()
    sys.stderr = captured_err = io.StringIO()

    result = {"status": "SUCCESS", "output": "", "error": ""}

    try:
        # Execute in a restricted namespace
        exec_globals = {"__builtins__": __builtins__}
        exec(code, exec_globals)

        stdout_val = captured_out.getvalue()
        stderr_val = captured_err.getvalue()

        result["output"] = stdout_val
        if stderr_val:
            result["output"] += f"\n[stderr]\n{stderr_val}"

    except Exception as e:
        result["status"] = "ERROR"
        result["error"] = traceback.format_exc()
        result["output"] = captured_out.getvalue()

    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr

    return result


# Entry point for sandbox execution
if __name__ == "__main__":
    import os
    args_json = os.environ.get("SKILL_ARGUMENTS", "{}")
    args = json.loads(args_json)
    result = run(args)
    # Output as JSON on the last line for the executor to parse
    print(json.dumps(result))
