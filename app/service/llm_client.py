from __future__ import annotations

import re
from typing import Any

import httpx
from pydantic import ValidationError

from app.dto import (
    BinaryCriteriaResult,
    CriteriaEvaluationResult,
    FeedbackResult,
    FewShotGradingResult,
)

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

    async def _request_content(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        array: bool = False,
        plain_text: bool = False,
    ) -> str:
        try:
            response = await self._client.post(
                self.url,
                json={
                    "model": model or self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": self.temperature,
                    "max_tokens": self.max_tokens,
                    **(
                        {}
                        if array or plain_text
                        else {"response_format": {"type": "json_object"}}
                    ),
                    "chat_template_kwargs": {"enable_thinking": False},
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
                f"LLM response must contain choices[0].message.content. failed: {exc}"
            ) from exc

        match = JSON_CODE_FENCE.fullmatch(content.strip())
        return match.group(1) if match else content

    async def grade(
        self, *, system_prompt: str, user_prompt: str, model: str | None = None
    ) -> CriteriaEvaluationResult:
        content = await self._request_content(
            system_prompt=system_prompt, user_prompt=user_prompt, model=model
        )
        try:
            return CriteriaEvaluationResult.model_validate_json(content)
        except ValidationError as exc:
            raise LLMResponseError(
                f"LLM returned an invalid grading result: {exc}"
            ) from exc

    async def grade_criteria(
        self, *, system_prompt: str, user_prompt: str, model: str | None = None
    ) -> CriteriaEvaluationResult:
        # Keep the explicit name while routing all rubric calls through the same
        # criterion-only parser, including callers of the original grade method.
        return await self.grade(
            system_prompt=system_prompt, user_prompt=user_prompt, model=model
        )

    async def generate_feedback(
        self, *, system_prompt: str, user_prompt: str, model: str
    ) -> FeedbackResult:
        content = await self._request_content(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            plain_text=True,
        )
        return FeedbackResult(feedback=content)

    async def grade_fewshot(
        self, *, system_prompt: str, user_prompt: str, model: str | None = None
    ) -> FewShotGradingResult:
        content = await self._request_content(
            system_prompt=system_prompt, user_prompt=user_prompt, model=model
        )
        try:
            return FewShotGradingResult.model_validate_json(content)
        except ValidationError as exc:
            raise LLMResponseError(
                f"LLM returned an invalid grading result: {exc}"
            ) from exc

    async def grade_binary(
        self,
        *,
        question: str,
        desired_answer: str,
        student_answer: str,
        criteria: list[str],
        model: str | None = None,
    ) -> list[int]:
        system = (
            "You are a strict grading assistant.\n\n"
            "Your task is to evaluate whether a student's answer fulfills each grading criterion.\n\n"
            "For each criterion:\n- Output 1 if the criterion is fulfilled.\n"
            "- Output 0 if the criterion is not fulfilled.\n\n"
            "Evaluate each criterion independently.\n\n"
            "Return ONLY a valid JSON array containing 0 or 1.\n"
            "The output array must contain exactly one value for each criterion, "
            "in the same order as the criteria provided.\n\n"
            "Do not include explanations.\nDo not include markdown.\n"
            "Do not include any text before or after the JSON array."
        )
        descriptions = "\n".join(f"{i}. {text}" for i, text in enumerate(criteria, 1))
        content = await self._request_content(
            system_prompt=system,
            model=model,
            array=True,
            user_prompt=f"Question:\n{question}\n\nDesired answer:\n{desired_answer}\n\nStudent answer:\n{student_answer}\n\nCriteria:\n{descriptions}",
        )
        try:
            scores = BinaryCriteriaResult.model_validate_json(content).root
        except ValidationError:
            raise LLMResponseError(
                "Grading model must return a nonempty JSON array of integer 0/1 labels"
            ) from None
        if len(scores) != len(criteria):
            raise LLMResponseError(
                "Candidate rubric labels must be aligned binary integers"
            )
        return scores
