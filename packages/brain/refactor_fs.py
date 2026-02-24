import os
import ast
from pathlib import Path

source = Path("agent/tools/host_files.py")
content = source.read_text(encoding="utf-8")

nodes = ast.parse(content)

# Group elements
imports = []
utils = []
read_funcs = []
write_funcs = []
explore_funcs = []
registry_funcs = []

for node in nodes.body:
    src = ast.get_source_segment(content, node)
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        imports.append(src)
    elif isinstance(node, ast.Assign):
        # Global assignments
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if any(t in ["BLOCKED_PATHS", "BLOCKED_EXTENSIONS", "_HOST_MOUNT_ROOT", "_CONTAINER_MOUNT_POINT", "TREE_SKIP_DIRS", "PROJECT_MARKERS"] for t in targets):
            utils.append(src)
        elif any(t in ["READ_CACHE_MAX_ENTRIES", "_read_cache"] for t in targets):
            read_funcs.append(src)
        elif any(t in ["TREE_CACHE_TTL_SECONDS", "TREE_CACHE_MAX_ENTRIES", "_tree_cache"] for t in targets):
            explore_funcs.append(src)
        else:
            utils.append(src) # e.g. logger
    elif isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
        name = node.name
        if name in ["_get_host_mounts", "_host_to_container_path", "_container_to_host_path", "_is_blocked_path", "_resolve_host_path", "_host_file_info"]:
            utils.append(src)
        elif name in ["host_read", "host_batch_read", "_read_text_cached"]:
            read_funcs.append(src)
        elif name in ["host_write"]:
            write_funcs.append(src)
        elif name in ["host_list", "host_search", "host_tree", "_build_host_tree_sync", "host_find", "project_recall"]:
            explore_funcs.append(src)
        elif name == "register_host_file_tools":
            registry_funcs.append(src)

import_block = "\n".join(imports)

utils_code = import_block + "\n\n" + "\n\n".join(utils)
utils_code = utils_code.replace("from agent.types import", "from agent.types import") # no-op

read_code = import_block + "\nfrom .utils import *\n\n" + "\n\n".join(read_funcs)
write_code = import_block + "\nfrom .utils import *\n\n" + "\n\n".join(write_funcs)
explore_code = import_block + "\nfrom .utils import *\n\n" + "\n\n".join(explore_funcs)

init_code = ""

host_files_code = f'''{import_block}

from .fs.read import host_read, host_batch_read
from .fs.write import host_write
from .fs.explore import host_list, host_search, host_tree, host_find, project_recall

{registry_funcs[0]}
'''

Path("agent/tools/fs").mkdir(exist_ok=True)
Path("agent/tools/fs/__init__.py").write_text(init_code)
Path("agent/tools/fs/utils.py").write_text(utils_code)
Path("agent/tools/fs/read.py").write_text(read_code)
Path("agent/tools/fs/write.py").write_text(write_code)
Path("agent/tools/fs/explore.py").write_text(explore_code)

source.write_text(host_files_code)

print("Split completed successfully.")
