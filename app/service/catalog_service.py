class CatalogService:
    """Course, exam, question, and rubric-mapping use cases."""

    def __init__(self, core) -> None:
        self.core = core

    def __getattr__(self, name: str):
        if name in {"create_course", "list_courses", "create_exam", "list_exams", "get_exam", "update_exam_rubric", "map_question_chunks"}:
            return getattr(self.core, name)
        raise AttributeError(name)
