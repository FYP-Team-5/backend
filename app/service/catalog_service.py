class CatalogService:
    """Course and test use cases."""

    def __init__(self, core) -> None:
        self.core = core

    def __getattr__(self, name: str):
        if name in {
            "create_course",
            "list_courses",
            "create_test",
            "list_tests",
            "get_test",
            "set_question_rubric",
        }:
            return getattr(self.core, name)
        raise AttributeError(name)
