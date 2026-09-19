class CatalogService:
    """Course and test use cases."""

    def __init__(self, core) -> None:
        self.core = core

    def __getattr__(self, name: str):
        if name in {
            "create_course",
            "list_courses",
            "create_test",
            "create_test_from_csv",
            "list_tests",
            "get_test",
            "set_question_rubric",
            "set_grading_method",
            "upload_criteria_csv",
            "upload_examples_csv",
            "grade_fewshot",
        }:
            return getattr(self.core, name)
        raise AttributeError(name)
