from app.controller.auth import auth_router
from app.controller.courses import courses_router
from app.controller.health import health_router
from app.controller.tests import tests_router
from app.controller.users import users_router

__all__ = [
    "auth_router",
    "courses_router",
    "health_router",
    "tests_router",
    "users_router",
]
