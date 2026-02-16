"""
Sandbox entrypoint — runs inside the Docker container.
Loads the skill, executes the target function, and outputs the result.
"""

import json
import os
import sys
import importlib.util
import traceback


def main():
    function_name = os.environ.get("SKILL_FUNCTION", "")
    arguments_json = os.environ.get("SKILL_ARGUMENTS", "{}")

    if not function_name:
        print(json.dumps({"error": "No SKILL_FUNCTION specified"}))
        sys.exit(1)

    try:
        arguments = json.loads(arguments_json)
    except json.JSONDecodeError:
        print(json.dumps({"error": "Invalid SKILL_ARGUMENTS JSON"}))
        sys.exit(1)

    # Load the skill module from /skill directory
    skill_dir = "/skill"
    init_path = os.path.join(skill_dir, "__init__.py")
    main_path = os.path.join(skill_dir, "main.py")

    module_path = main_path if os.path.exists(main_path) else init_path

    if not os.path.exists(module_path):
        print(json.dumps({"error": f"No skill module found at {module_path}"}))
        sys.exit(1)

    try:
        spec = importlib.util.spec_from_file_location("skill", module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["skill"] = module
        spec.loader.exec_module(module)
    except Exception as e:
        print(json.dumps({"error": f"Failed to load skill: {str(e)}"}))
        sys.exit(1)

    # Find and call the function
    func = getattr(module, function_name, None)
    if func is None:
        print(json.dumps({"error": f"Function '{function_name}' not found in skill"}))
        sys.exit(1)

    try:
        result = func(arguments)
        print(json.dumps(result if isinstance(result, dict) else {"result": str(result)}))
    except Exception as e:
        print(json.dumps({
            "error": str(e),
            "traceback": traceback.format_exc(),
        }))
        sys.exit(1)


if __name__ == "__main__":
    main()
