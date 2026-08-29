from __future__ import annotations

import re
from typing import Any

import httpx
from pydantic import ValidationError

from app.dto import GradingResult

JSON_CODE_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


class LLMServiceError(RuntimeError):
    """Raised when the configured local LLM cannot be reached."""


class LLMResponseError(LLMServiceError):
    """Raised when the local LLM returns an invalid grading result."""


class LocalLLMClient:
    def __init__(
        self,
        *,
        url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 120,
        max_retries: int = 1,
        temperature: float = 0,
        max_tokens: int = 2000,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._owns_client = client is None
        if client is not None:
            self._client = client
            return

        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            transport=httpx.AsyncHTTPTransport(retries=max_retries),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def health(self) -> bool:
        models_url = self.url.rsplit("/chat/completions", 1)[0] + "/models"
        try:
            response = await self._client.get(models_url)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def grade(self, *, system_prompt: str, user_prompt: str) -> GradingResult:
        try:
            response = await self._client.post(
                self.url,
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": self.temperature,
                    "max_tokens": self.max_tokens,
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMServiceError(f"LLM request to {self.url} failed: {exc}") from exc

        try:
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise TypeError("message content is empty")
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError(
                "LLM response must contain choices[0].message.content."
            ) from exc

        match = JSON_CODE_FENCE.fullmatch(content.strip())
        if match:
            content = match.group(1)
        try:
            return GradingResult.model_validate_json(content)
        except ValidationError as exc:
            raise LLMResponseError(f"LLM returned an invalid grading result: {exc}") from exc
