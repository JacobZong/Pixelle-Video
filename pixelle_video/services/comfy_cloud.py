# Copyright (C) 2025 AIDC-AI
#
# Licensed under the Apache License, Version 2.0 (the "License");

"""
ComfyUI Cloud executor.

This keeps ComfyUI Cloud separate from self-hosted ComfyUI because Cloud uses
different auth headers and job polling endpoints even though the submitted
workflow JSON is still ComfyUI API format.
"""

import asyncio
import json
import mimetypes
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import aiohttp
from loguru import logger

from comfykit.comfyui.base_executor import ComfyUIExecutor
from comfykit.comfyui.models import ExecuteResult


class ComfyCloudExecutor(ComfyUIExecutor):
    """Execute API-format workflows on ComfyUI Cloud."""

    COMPLETED_STATUSES = {"completed", "success"}
    FAILED_STATUSES = {"failed", "error", "cancelled", "canceled"}

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 600,
        poll_interval: float = 2.0,
    ):
        final_base_url = self.normalize_base_url(base_url)
        final_api_key = api_key or os.getenv("COMFY_CLOUD_API_KEY")
        if not final_api_key:
            raise ValueError("ComfyUI Cloud API key is required")

        super().__init__(base_url=final_base_url, api_key=final_api_key)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._object_info_cache: Optional[Dict[str, Any]] = None

    @staticmethod
    def normalize_base_url(base_url: Optional[str] = None) -> str:
        """Normalize both https://cloud.comfy.org and .../api forms."""
        url = (base_url or os.getenv("COMFY_CLOUD_BASE_URL") or "https://cloud.comfy.org/api").rstrip("/")
        if not url.endswith("/api"):
            url = f"{url}/api"
        return url

    @asynccontextmanager
    async def get_comfyui_session(self) -> AsyncGenerator[aiohttp.ClientSession, None]:
        """Create an aiohttp session with ComfyUI Cloud auth."""
        timeout = aiohttp.ClientTimeout(total=max(self.timeout, 30))
        headers = {"X-API-Key": self.api_key}
        async with aiohttp.ClientSession(headers=headers, timeout=timeout, trust_env=True) as session:
            yield session

    async def execute_workflow(self, workflow_file: str, params: Dict[str, Any] = None) -> ExecuteResult:
        """Execute a workflow file on ComfyUI Cloud."""
        try:
            workflow_path = Path(workflow_file)
            if not workflow_path.exists():
                return ExecuteResult(status="error", msg=f"Workflow file does not exist: {workflow_file}")

            metadata = self.get_workflow_metadata(str(workflow_path))
            if not metadata:
                return ExecuteResult(status="error", msg="Cannot parse workflow metadata")

            with workflow_path.open("r", encoding="utf-8") as f:
                workflow_data = json.load(f)

            workflow_data = await self._apply_params_to_workflow(workflow_data, metadata, params or {})
            workflow_data, _ = self._randomize_seed_in_workflow(workflow_data)
            self._validate_cloud_nodes_available(workflow_data, await self._get_object_info())

            output_id_2_var = self._extract_output_nodes(metadata)
            client_id = str(uuid.uuid4())
            prompt_id = await self._submit_workflow(workflow_data, client_id)
            return await self._wait_for_result(prompt_id, output_id_2_var)

        except Exception as e:
            logger.error(f"ComfyUI Cloud workflow execution failed: {e}")
            return ExecuteResult(status="error", msg=str(e))

    async def test_connection(self) -> Tuple[bool, str]:
        """Test Cloud reachability without submitting a workflow."""
        try:
            user = await self._request_json("GET", "/user")
            queue = await self._request_json("GET", "/queue")
            object_info = await self._get_object_info()
            return True, f"user={bool(user)} queue={bool(queue)} nodes={len(object_info)}"
        except Exception as e:
            return False, str(e)

    async def _request_json(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        allow_redirects: bool = True,
    ) -> Any:
        url = f"{self.base_url}{path}"
        async with self.get_comfyui_session() as session:
            async with session.request(
                method,
                url,
                json=payload,
                allow_redirects=allow_redirects,
            ) as response:
                if response.status < 200 or response.status >= 300:
                    body = await response.text()
                    raise Exception(f"ComfyUI Cloud API {method} {path} failed: HTTP {response.status}: {body[:500]}")
                return await response.json()

    async def _get_object_info(self) -> Dict[str, Any]:
        if self._object_info_cache is None:
            object_info = await self._request_json("GET", "/object_info")
            if not isinstance(object_info, dict):
                raise Exception("ComfyUI Cloud /object_info returned an invalid response")
            self._object_info_cache = object_info
        return self._object_info_cache

    def _validate_cloud_nodes_available(self, workflow_data: Dict[str, Any], object_info: Dict[str, Any]) -> None:
        required_nodes = sorted(
            {
                node.get("class_type")
                for node in workflow_data.values()
                if isinstance(node, dict) and node.get("class_type")
            }
        )
        missing_nodes = [node for node in required_nodes if node not in object_info]
        if missing_nodes:
            raise Exception(
                "ComfyUI Cloud is missing required node types: "
                + ", ".join(missing_nodes)
            )

    async def _submit_workflow(self, workflow_data: Dict[str, Any], client_id: str) -> str:
        payload = {
            "prompt": workflow_data,
            "client_id": client_id,
            "extra_data": {
                "api_key_comfy_org": self.api_key,
            },
        }
        result = await self._request_json("POST", "/prompt", payload)
        prompt_id = result.get("prompt_id")
        if not prompt_id:
            raise Exception(f"ComfyUI Cloud did not return prompt_id: {result}")
        logger.info(f"ComfyUI Cloud job submitted: {prompt_id}")
        return prompt_id

    async def _wait_for_result(
        self,
        prompt_id: str,
        output_id_2_var: Optional[Dict[str, str]] = None,
    ) -> ExecuteResult:
        start_time = time.time()

        while True:
            elapsed = time.time() - start_time
            if elapsed > self.timeout:
                return ExecuteResult(
                    status="timeout",
                    prompt_id=prompt_id,
                    duration=elapsed,
                    msg=f"ComfyUI Cloud job timed out after {self.timeout}s",
                )

            status_info = await self._request_json("GET", f"/job/{prompt_id}/status")
            status = str(status_info.get("status") or "").lower()

            if status in self.COMPLETED_STATUSES:
                job = await self._request_json("GET", f"/jobs/{prompt_id}")
                result = await self._build_result_from_job(job, output_id_2_var or {})
                result.prompt_id = prompt_id
                result.duration = elapsed
                return result

            if status in self.FAILED_STATUSES:
                msg = status_info.get("error_message") or status_info.get("message") or f"ComfyUI Cloud job {status}"
                try:
                    job = await self._request_json("GET", f"/jobs/{prompt_id}")
                    execution_error = job.get("execution_error") if isinstance(job, dict) else None
                    if execution_error:
                        msg = execution_error.get("exception_message") or msg
                except Exception:
                    pass
                return ExecuteResult(status="error", prompt_id=prompt_id, duration=elapsed, msg=msg)

            await asyncio.sleep(self.poll_interval)

    async def _build_result_from_job(
        self,
        job: Dict[str, Any],
        output_id_2_var: Dict[str, str],
    ) -> ExecuteResult:
        outputs = job.get("outputs") or job.get("preview_output") or {}
        result = ExecuteResult(status="completed", outputs=outputs)

        output_id_2_images: Dict[str, List[str]] = {}
        output_id_2_videos: Dict[str, List[str]] = {}
        output_id_2_audios: Dict[str, List[str]] = {}
        output_id_2_texts: Dict[str, List[str]] = {}

        for node_id, node_output in outputs.items():
            if not isinstance(node_output, dict):
                continue
            images, videos, audios = await self._split_cloud_media_by_suffix(node_output)
            if images:
                output_id_2_images[node_id] = images
            if videos:
                output_id_2_videos[node_id] = videos
            if audios:
                output_id_2_audios[node_id] = audios
            if "text" in node_output:
                texts = node_output["text"]
                if isinstance(texts, str):
                    texts = [texts]
                elif not isinstance(texts, list):
                    texts = [str(texts)]
                output_id_2_texts[node_id] = texts

        if output_id_2_images:
            result.images_by_var = self._map_outputs_by_var(output_id_2_var, output_id_2_images)
            result.images = self._extend_flat_list_from_dict(result.images_by_var)
        if output_id_2_videos:
            result.videos_by_var = self._map_outputs_by_var(output_id_2_var, output_id_2_videos)
            result.videos = self._extend_flat_list_from_dict(result.videos_by_var)
        if output_id_2_audios:
            result.audios_by_var = self._map_outputs_by_var(output_id_2_var, output_id_2_audios)
            result.audios = self._extend_flat_list_from_dict(result.audios_by_var)
        if output_id_2_texts:
            result.texts_by_var = self._map_outputs_by_var(output_id_2_var, output_id_2_texts)
            result.texts = self._extend_flat_list_from_dict(result.texts_by_var)

        return result

    async def _split_cloud_media_by_suffix(
        self,
        node_output: Dict[str, Any],
    ) -> Tuple[List[str], List[str], List[str]]:
        image_exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}
        video_exts = {".mp4", ".mov", ".avi", ".webm", ".gif"}
        audio_exts = {".mp3", ".wav", ".flac", ".ogg", ".aac", ".m4a", ".wma", ".opus"}

        images: List[str] = []
        videos: List[str] = []
        audios: List[str] = []

        for media_key in ("images", "gifs", "audio"):
            for media_data in node_output.get(media_key, []):
                file_info = self._normalize_file_info(media_data)
                if not file_info:
                    continue

                url = await self._get_signed_view_url(file_info)
                filename = file_info.get("filename", "")
                ext = os.path.splitext(filename)[1].lower()
                if ext in image_exts:
                    images.append(url)
                elif ext in video_exts:
                    videos.append(url)
                elif ext in audio_exts:
                    audios.append(url)

        return images, videos, audios

    def _normalize_file_info(self, media_data: Any) -> Optional[Dict[str, str]]:
        if isinstance(media_data, dict):
            if media_data.get("url"):
                return media_data
            if media_data.get("filename"):
                return {
                    "filename": media_data.get("filename", ""),
                    "subfolder": media_data.get("subfolder", ""),
                    "type": media_data.get("type", "output"),
                }
        elif isinstance(media_data, str):
            return {"filename": media_data, "subfolder": "", "type": "output"}
        return None

    async def _get_signed_view_url(self, file_info: Dict[str, str]) -> str:
        if file_info.get("url"):
            return file_info["url"]

        query = urlencode(
            {
                "filename": file_info.get("filename", ""),
                "subfolder": file_info.get("subfolder", ""),
                "type": file_info.get("type", "output"),
            }
        )
        view_url = f"{self.base_url}/view?{query}"

        async with self.get_comfyui_session() as session:
            async with session.get(view_url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if location:
                        return location
                if response.status == 200:
                    logger.warning("ComfyUI Cloud /view returned 200 instead of a signed redirect")
                    return view_url
                body = await response.text()
                raise Exception(f"ComfyUI Cloud view failed: HTTP {response.status}: {body[:500]}")

    async def _upload_media(self, media_path: str) -> str:
        with open(media_path, "rb") as f:
            media_data = f.read()

        filename = os.path.basename(media_path)
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        data = aiohttp.FormData()
        data.add_field("image", media_data, filename=filename, content_type=mime_type)
        data.add_field("type", "input")
        data.add_field("overwrite", "true")

        async with self.get_comfyui_session() as session:
            async with session.post(f"{self.base_url}/upload/image", data=data) as response:
                if response.status != 200:
                    body = await response.text()
                    raise Exception(f"ComfyUI Cloud upload failed: HTTP {response.status}: {body[:500]}")

                result = await response.json()
                return result.get("name") or result.get("filename") or result.get("file", {}).get("name", "")
