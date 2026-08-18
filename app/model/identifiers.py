import re

# RAG and grading use this format for course, exam, rubric, question, and
# student identity hand-off values. A generated User ID is a UUID string and is
# deliberately a strict subset of this shared external-ID contract.
CROSS_SERVICE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
USER_ID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)


def is_cross_service_id(value: str) -> bool:
    return re.fullmatch(CROSS_SERVICE_ID_PATTERN, value) is not None
