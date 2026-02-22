import json
import logging

logger = logging.getLogger(__name__)

async def parse_nl_cron(prompt: str, provider, model: str, api_key: str) -> dict:
    """
    Parses natural language (e.g., 'every morning at 9am to check emails') into a cron schedule.
    Returns dict: { "cron": "0 9 * * *", "human_schedule": "Daily at 9:00 AM", "task": "..." }
    """
    system_instruction = (
        "You are an expert at cron scheduling. Given a user's natural language request, "
        "extract the intended action and return a valid 5-part cron expression (minute hour day month day-of-week).\n\n"
        "Return ONLY a raw JSON object with exactly these 3 keys:\n"
        '- "cron": a valid standard cron expression (e.g. "0 9 * * 1-5")\n'
        '- "human_schedule": a clear, brief human-readable version (e.g. "Every weekday at 9:00 AM")\n'
        '- "task": the core task or goal they want to accomplish (e.g. "Check my unread emails")\n\n'
        "If the user does not specify a clear time, guess a reasonable default.\n"
        "Do not wrap inside markdown code blocks. Output raw JSON only."
    )

    messages = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": prompt}
    ]

    response_chunks = []
    try:
        async for token in provider.stream(
            messages=messages,
            model=model,
            temperature=0.1,  # low temp for deterministic JSON
            max_tokens=300,
            api_key=api_key
        ):
            response_chunks.append(token)
            
        raw_response = "".join(response_chunks).strip()
        
        # Clean up markdown if LLM disobeyed
        if raw_response.startswith("```json"):
            raw_response = raw_response[7:]
        if raw_response.startswith("```"):
            raw_response = raw_response[3:]
        if raw_response.endswith("```"):
            raw_response = raw_response[:-3]
            
        raw_response = raw_response.strip()
        data = json.loads(raw_response)
        
        # Ensure we have the required keys
        return {
            "cron": data.get("cron", "0 0 * * *"),
            "human_schedule": data.get("human_schedule", "Daily at midnight"),
            "task": data.get("task", prompt)
        }
        
    except json.JSONDecodeError as de:
        logger.error(f"Failed to parse JSON from LLM: {raw_response[:100]}... - {de}")
        return {
            "cron": "0 0 * * *",
            "human_schedule": "Daily at midnight (fallback)",
            "task": prompt
        }
    except Exception as e:
        logger.error(f"Error calling provider for cron parser: {e}")
        return {
            "cron": "0 0 * * *", 
            "human_schedule": "Daily at midnight (error fallback)",
            "task": prompt
        }
