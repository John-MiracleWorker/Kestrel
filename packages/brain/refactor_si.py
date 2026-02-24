import os
import ast
from pathlib import Path

source = Path("agent/tools/self_improve.py")
content = source.read_text(encoding="utf-8")

nodes = ast.parse(content)

imports = []
utils = []
ast_analyzer = []
github_sync = []
proposals = []
registry_main = []

for node in nodes.body:
    src = ast.get_source_segment(content, node)
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        imports.append(src)
    elif isinstance(node, ast.Assign):
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if any(t in ["_SECRET_RE", "_codebase_overview_cache", "_CODEBASE_OVERVIEW_TTL"] for t in targets):
            ast_analyzer.append(src)
        elif any(t in ["_SYNCED_ISSUES_FILE", "_SEVERITY_LABELS"] for t in targets):
            github_sync.append(src)
        elif any(t in ["_SCHEDULER_INTERVAL_HOURS", "_scheduler_started"] for t in targets):
            registry_main.append(src)
        else:
            utils.append(src)
    elif isinstance(node, ast.AnnAssign):
        utils.append(src)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        name = node.name
        if name in ["_analyze_file_content", "_process_file", "_deep_scan"]:
            ast_analyzer.append(src)
        elif name in ["_github_sync", "_load_synced_hashes", "_save_synced_hashes", "_issue_hash"]:
            github_sync.append(src)
        elif name in ["_run_tests", "_propose_improvements", "_llm_analyze", "_handle_approval", "_telegram_digest"]:
            proposals.append(src)
        elif name in ["self_improve_action", "register_self_improve_tools", "start_scheduler", "_scheduled_health_check"]:
            registry_main.append(src)
        else:
            utils.append(src)

import_block = "\n".join(imports)

utils_code = import_block + "\n\n" + "\n\n".join(utils)
ast_code = import_block + "\nfrom .utils import *\n\n" + "\n\n".join(ast_analyzer)
github_code = import_block + "\nfrom .utils import *\nfrom .ast_analyzer import _deep_scan\n\n" + "\n\n".join(github_sync)
proposals_code = import_block + "\nfrom .utils import *\nfrom .ast_analyzer import _deep_scan\n\n" + "\n\n".join(proposals)

host_files_code = f'''{import_block}

from .self_improvement.utils import *
from .self_improvement.ast_analyzer import _deep_scan, _CODEBASE_OVERVIEW_TTL, _codebase_overview_cache
from .self_improvement.github_sync import _github_sync
from .self_improvement.proposals import _run_tests, _propose_improvements, _telegram_digest, _handle_approval

{chr(10).join(registry_main)}
'''

Path("agent/tools/self_improvement").mkdir(exist_ok=True)
Path("agent/tools/self_improvement/__init__.py").write_text("")
Path("agent/tools/self_improvement/utils.py").write_text(utils_code)
Path("agent/tools/self_improvement/ast_analyzer.py").write_text(ast_code)
Path("agent/tools/self_improvement/github_sync.py").write_text(github_code)
Path("agent/tools/self_improvement/proposals.py").write_text(proposals_code)

source.write_text(host_files_code)

print("Self Improve split completed successfully.")
