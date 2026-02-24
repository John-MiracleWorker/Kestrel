import os
import re

chat_py = "packages/brain/services/chat_service.py"

with open(chat_py, "r") as f:
    content = f.read()

replacements = {
    "_retrieval": "runtime.retrieval",
    "_embedding_pipeline": "runtime.embedding_pipeline",
    "_vector_store": "runtime.vector_store",
    "_agent_persistence": "runtime.agent_persistence",
    "_command_parser": "runtime.command_parser",
    "_hands_client": "runtime.hands_client",
    "_memory_graph": "runtime.memory_graph",
    "_cron_scheduler": "runtime.cron_scheduler",
    "_persona_learner": "runtime.persona_learner",
    "_skill_manager": "runtime.skill_manager",
}

for old, new in replacements.items():
    # Only replace whole words (not parts of other variables)
    content = re.sub(rf'\b{old}\b', new, content)

with open(chat_py, "w") as f:
    f.write(content)
print("Globals replaced.")
