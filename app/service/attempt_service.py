class AttemptService:
    """Attempt submission, grading, and result-query use cases."""

    def __init__(self, core) -> None:
        self.core = core

    def __getattr__(self, name: str):
        if name in {"create_attempt", "list_attempts", "grade_attempt", "get_attempt_result"}:
            return getattr(self.core, name)
        raise AttributeError(name)
