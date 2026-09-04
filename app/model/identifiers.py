import re

# RAG and grading use this format for course, exam, rubric, question, and
# student identity hand-off values.
CROSS_SERVICE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
USER_ID_PATTERN = r"^[0-9]+$"


def is_cross_service_id(value: str) -> bool:
    return re.fullmatch(CROSS_SERVICE_ID_PATTERN, value) is not None
