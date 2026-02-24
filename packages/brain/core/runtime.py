from typing import Optional, Dict, Any

# These globals hold exactly the same state initialized in server.py serve()
retrieval = None
embedding_pipeline = None
vector_store = None

agent_loop = None
agent_persistence = None
running_tasks: Dict[str, Any] = {}

hands_client = None
cron_scheduler = None
webhook_handler = None
memory_graph = None
tool_registry = None
persona_learner = None
task_predictor = None
command_parser = None
metrics_collector = None
workflow_registry = None
skill_manager = None
session_manager = None
sandbox_manager = None
