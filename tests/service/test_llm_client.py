import asyncio
import json

import httpx
import pytest

from app.service import LLMResponseError, LocalLLMClient


def test_grade_uses_openai_contract_and_parses_structured_result() -> None:
    async def exercise() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert request.url.path == "/v1/chat/completions"
            assert body["model"] == "grader"
            assert body["response_format"] == {"type": "json_object"}
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "score": 4,
                                        "max_score": 5,
                                        "feedback": "Good work.",
                                        "criteria": [],
                                    }
                                )
                            }
                        }
                    ]
                },
            )

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = LocalLLMClient(
            url="http://llm.test/v1/chat/completions",
            model="grader",
            client=http_client,
        )

        result = await client.grade(system_prompt="system", user_prompt="user")

        assert result.score == 4
        assert result.max_score == 5
        await http_client.aclose()

    asyncio.run(exercise())


def test_grade_rejects_invalid_structured_result() -> None:
    async def exercise() -> None:
        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "content": (
                                        '{"score": 8, "max_score": 5, '
                                        '"feedback": "bad"}'
                                    )
                                }
                            }
                        ]
                    },
                )
            )
        )
        client = LocalLLMClient(
            url="http://llm.test/v1/chat/completions",
            model="grader",
            client=http_client,
        )

        with pytest.raises(LLMResponseError, match="invalid grading result"):
            await client.grade(system_prompt="system", user_prompt="user")
        await http_client.aclose()

    asyncio.run(exercise())
