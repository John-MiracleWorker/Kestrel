"""
Swarm Client — async HTTP wrapper for ComfyUI REST API.

Handles job submission, history polling, and media download from the
remote Windows host running SwarmUI (ComfyUI backend).
"""

import json
import logging
from typing import Any, Optional

import aiohttp

from agent.tools.media_gen.config import (
    CLIENT_ID,
    HTTP_TIMEOUT,
    SWARM_BASE_URL,
)

logger = logging.getLogger("brain.agent.tools.media_gen.swarm_client")


class SwarmClient:
    """Async client for ComfyUI/SwarmUI REST API."""

    def __init__(self, base_url: str = None, client_id: str = None):
        self.base_url = (base_url or SWARM_BASE_URL).rstrip("/")
        self.client_id = client_id or CLIENT_ID
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Lazily create and return the HTTP session."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def submit_job(self, workflow_json: dict) -> str:
        """
        Submit a ComfyUI workflow for execution.

        Args:
            workflow_json: The complete ComfyUI API-format workflow dict.

        Returns:
            The prompt_id string assigned by ComfyUI.

        Raises:
            ConnectionError: If the host is unreachable.
            RuntimeError: If the API returns an error.
        """
        session = await self._get_session()
        url = f"{self.base_url}/prompt"

        payload = {
            "prompt": workflow_json,
            "client_id": self.client_id,
        }

        try:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(
                        f"ComfyUI /prompt returned {resp.status}: {body}"
                    )
                data = await resp.json()
                prompt_id = data.get("prompt_id")
                if not prompt_id:
                    raise RuntimeError(f"No prompt_id in response: {data}")
                logger.info(f"Job submitted: prompt_id={prompt_id}")
                return prompt_id

        except aiohttp.ClientConnectorError as e:
            raise ConnectionError(
                f"Cannot reach SwarmUI at {self.base_url}. "
                f"Is the host PC awake and SwarmUI running? Error: {e}"
            ) from e

    async def get_history(self, prompt_id: str) -> dict[str, Any]:
        """
        Fetch the execution history/metadata for a completed job.

        Args:
            prompt_id: The ID returned by submit_job().

        Returns:
            The history dict for this prompt.
        """
        session = await self._get_session()
        url = f"{self.base_url}/history/{prompt_id}"

        async with session.get(url) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(
                    f"ComfyUI /history returned {resp.status}: {body}"
                )
            data = await resp.json()
            return data.get(prompt_id, {})

    async def download_media(self, filename: str, subfolder: str = "", folder_type: str = "output") -> bytes:
        """
        Download the generated media artifact from ComfyUI.

        Args:
            filename: The output filename from the history metadata.
            subfolder: Optional subfolder path within the output directory.
            folder_type: The folder type (usually "output").

        Returns:
            The raw file bytes.
        """
        session = await self._get_session()
        params = {
            "filename": filename,
            "subfolder": subfolder,
            "type": folder_type,
        }
        url = f"{self.base_url}/view"

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(
                        f"ComfyUI /view returned {resp.status}: {body}"
                    )
                data = await resp.read()
                logger.info(f"Downloaded {filename} ({len(data):,} bytes)")
                return data

        except aiohttp.ClientConnectorError as e:
            raise ConnectionError(
                f"Cannot download media from {self.base_url}: {e}"
            ) from e

    async def get_system_stats(self) -> dict:
        """Fetch system stats (GPU info, queue status) from ComfyUI."""
        session = await self._get_session()
        url = f"{self.base_url}/system_stats"

        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return {"error": f"Status {resp.status}"}
                return await resp.json()
        except aiohttp.ClientConnectorError:
            return {"error": "Host unreachable"}

    async def get_queue(self) -> dict:
        """Fetch the current generation queue from ComfyUI."""
        session = await self._get_session()
        url = f"{self.base_url}/queue"

        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return {"error": f"Status {resp.status}"}
                return await resp.json()
        except aiohttp.ClientConnectorError:
            return {"error": "Host unreachable"}
