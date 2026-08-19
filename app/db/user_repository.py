from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    MetaData,
    String,
    Table,
    create_engine,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.dto import UserResponse
from app.model import Staff, Student

metadata = MetaData()

users = Table(
    "users",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("email", String(320), nullable=False, unique=True, index=True),
    Column("full_name", String(300), nullable=False),
    Column("role", String(16), nullable=False, index=True),
    Column("password_hash", String(512), nullable=False),
    Column("active", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

student_profiles = Table(
    "student_profiles",
    metadata,
    Column("user_id", ForeignKey("users.id"), primary_key=True),
    Column("student_number", String(64), nullable=False, unique=True, index=True),
)

staff_profiles = Table(
    "staff_profiles",
    metadata,
    Column("user_id", ForeignKey("users.id"), primary_key=True),
    Column("staff_number", String(64), nullable=False, unique=True, index=True),
)


class UserStoreError(RuntimeError):
    pass


class UserNotFoundError(KeyError):
    pass


class UserConflictError(ValueError):
    pass


class PostgresUserRepository:
    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
    ) -> None:
        if database_url is None and engine is None:
            raise ValueError("database_url is required when engine is not provided.")
        self.engine = engine or create_engine(database_url, pool_pre_ping=True)

    def initialize(self) -> None:
        metadata.create_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()

    def health(self) -> bool:
        try:
            with self.engine.connect() as connection:
                connection.execute(select(func.count()).select_from(users))
            return True
        except SQLAlchemyError:
            return False

    def create_student(
        self,
        *,
        email: str,
        full_name: str,
        student_number: str,
        password_hash: str,
    ) -> Student:
        return self._create(
            email=email,
            full_name=full_name,
            role="student",
            institutional_number=student_number,
            password_hash=password_hash,
        )

    def create_staff(
        self,
        *,
        email: str,
        full_name: str,
        staff_number: str,
        password_hash: str,
    ) -> Staff:
        return self._create(
            email=email,
            full_name=full_name,
            role="staff",
            institutional_number=staff_number,
            password_hash=password_hash,
        )

    def _create(
        self,
        *,
        email: str,
        full_name: str,
        role: str,
        institutional_number: str,
        password_hash: str,
    ) -> UserResponse:
        user_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    insert(users).values(
                        id=user_id,
                        email=email,
                        full_name=full_name,
                        role=role,
                        password_hash=password_hash,
                        active=True,
                        created_at=now,
                        updated_at=now,
                    )
                )
                if role == "student":
                    connection.execute(
                        insert(student_profiles).values(
                            user_id=user_id,
                            student_number=institutional_number,
                        )
                    )
                else:
                    connection.execute(
                        insert(staff_profiles).values(
                            user_id=user_id,
                            staff_number=institutional_number,
                        )
                    )
        except IntegrityError as exc:
            raise UserConflictError(
                "Email or institutional number is already registered."
            ) from exc
        except SQLAlchemyError as exc:
            raise UserStoreError("Unable to create user.") from exc
        return self.get(user_id)[0]

    def get(self, user_id: str) -> tuple[UserResponse, str]:
        return self._get(users.c.id == user_id)

    def get_by_email(self, email: str) -> tuple[UserResponse, str]:
        return self._get(users.c.email == email)

    def _get(self, condition) -> tuple[UserResponse, str]:
        try:
            with self.engine.connect() as connection:
                row = (
                    connection.execute(select(users).where(condition))
                    .mappings()
                    .first()
                )
                if row is None:
                    raise UserNotFoundError
                profile = self._profile_number(connection, row)
        except UserNotFoundError:
            raise
        except SQLAlchemyError as exc:
            raise UserStoreError("Unable to read user data.") from exc
        return self._to_model(row, profile), str(row["password_hash"])

    def list(self, *, role: str | None = None) -> list[UserResponse]:
        statement = select(users).order_by(users.c.created_at, users.c.id)
        if role is not None:
            statement = statement.where(users.c.role == role)
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(statement).mappings().all()
                return [
                    self._to_model(row, self._profile_number(connection, row))
                    for row in rows
                ]
        except SQLAlchemyError as exc:
            raise UserStoreError("Unable to list users.") from exc

    def set_active(self, user_id: str, active: bool) -> UserResponse:
        try:
            with self.engine.begin() as connection:
                result = connection.execute(
                    update(users)
                    .where(users.c.id == user_id)
                    .values(active=active, updated_at=datetime.now(UTC))
                )
        except SQLAlchemyError as exc:
            raise UserStoreError("Unable to update user status.") from exc
        if result.rowcount == 0:
            raise UserNotFoundError(user_id)
        return self.get(user_id)[0]

    @staticmethod
    def _profile_number(connection, row: RowMapping) -> str:
        if row["role"] == "student":
            value = connection.execute(
                select(student_profiles.c.student_number).where(
                    student_profiles.c.user_id == row["id"]
                )
            ).scalar_one_or_none()
        else:
            value = connection.execute(
                select(staff_profiles.c.staff_number).where(
                    staff_profiles.c.user_id == row["id"]
                )
            ).scalar_one_or_none()
        if value is None:
            raise UserStoreError("User profile is incomplete.")
        return str(value)

    @staticmethod
    def _to_model(row: RowMapping, institutional_number: str) -> UserResponse:
        values = {
            key: value for key, value in dict(row).items() if key != "password_hash"
        }
        if row["role"] == "student":
            return Student(**values, student_number=institutional_number)
        return Staff(**values, staff_number=institutional_number)
