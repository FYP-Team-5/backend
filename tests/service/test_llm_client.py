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
                                        "feedback": "Good work.",
                                        "criteria_met": [
                                            {"criteria_id": "c1", "is_met": True}
                                        ],
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
        assert result.feedback == "Good work."
        assert result.criteria_met[0].criteria_id == "c1"
        assert result.criteria_met[0].is_met is True
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
                                    "content": ('{"score": -1, "feedback": "bad"}')
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


@pytest.mark.parametrize(
    "content",
    [
        "[true, 0]",
        "[1.0, 0]",
        "[2, 0]",
        "[-1, 0]",
        "[1]",
        "[1, 0, 1]",
        '{"criteria_met": [1, 0]}',
    ],
)
def test_binary_grading_rejects_nonbinary_or_misaligned_output(content):
    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"choices": [{"message": {"content": content}}]}
                )
            )
        ) as transport:
            client = LocalLLMClient(
                url="http://llm.test/v1/chat/completions",
                model="grading",
                client=transport,
            )
            with pytest.raises(LLMResponseError):
                await client.grade_binary(
                    question="Question",
                    desired_answer="Reference",
                    student_answer="Answer",
                    criteria=["Accuracy", "Evidence"],
                )

    asyncio.run(exercise())


def test_feedback_uses_separate_model_and_training_plain_text_contract():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "  You explain the concept well. Add a concrete example.  "
                        }
                    }
                ]
            },
        )

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ) as transport:
            client = LocalLLMClient(
                url="http://llm.test/v1/chat/completions",
                model="grading",
                client=transport,
            )
            result = await client.generate_feedback(
                system_prompt="Tutor",
                user_prompt="1. [MET] Concept\n2. [NOT MET] Example",
                model="feedback",
            )
            assert (
                result.feedback
                == "You explain the concept well. Add a concrete example."
            )

    asyncio.run(exercise())
    assert requests[0]["model"] == "feedback"
    assert "response_format" not in requests[0]
    assert requests[0]["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.parametrize("content", ["", " \n "])
def test_feedback_rejects_empty_content(content):
    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"choices": [{"message": {"content": content}}]}
                )
            )
        ) as transport:
            client = LocalLLMClient(
                url="http://llm.test/v1/chat/completions",
                model="grading",
                client=transport,
            )
            with pytest.raises(LLMResponseError):
                await client.generate_feedback(
                    system_prompt="Tutor", user_prompt="Answer", model="feedback"
                )

    asyncio.run(exercise())
