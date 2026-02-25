import os
import json
import logging
from datetime import datetime
from agent.tools.moltbook import post_to_moltbook

logger = logging.getLogger("brain.agent.tools.moltbook_reporter")

def generate_daily_report(stats: dict) -> str:
    """Generate a formatted report for Moltbook."""
    report = f"📊 **Daily AI Operations Report** - {datetime.now().strftime('%Y-%m-%d')}\n\n"
    report += f"✅ Tasks Completed: {stats.get('tasks_completed', 0)}\n"
    report += f"🛠 Tools Created: {stats.get('tools_created', 0)}\n"
    report += f"🧠 Memory Entries: {stats.get('memory_entries', 0)}\n\n"
    report += "Highlights:\n"
    for highlight in stats.get('highlights', []):
        report += f"- {highlight}\n"
    
    report += "\n#Kestrel #LibreBird #AI #DevLog"
    return report

async def post_daily_report(stats: dict):
    """Post the daily report to the 'tech' submolt."""
    content = generate_daily_report(stats)
    return await post_to_moltbook(
        title=f"Kestrel Ops Report: {datetime.now().strftime('%Y-%m-%d')}",
        content=content,
        submolt="tech"
    )
