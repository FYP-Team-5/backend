# User Identity Service

FastAPI microservice for student and staff accounts, authentication, and identity administration. It is the source of truth for people in the assessment system; RAG remains the source of truth for rubric files and chunks, while grading remains the source of truth for courses, exams, attempts, answers, grades, and AI feedback.

The service runs on port `8002` by default and exposes interactive documentation at `http://localhost:8002/docs`.

## Responsibilities and service boundaries

| Service | Owns | Cross-service identifiers |
|---|---|---|
| User | Users, student/staff profiles, password hashes, account status | Stable UUID in `User.id` and JWT `sub` |
| RAG | Rubric metadata, uploaded files, processing status, embeddings, Qdrant chunks | `course_id`, `exam_id`, `rubric_id` |
| Grading | Courses, exams/quizzes, questions, rubric mappings, attempts, answers, scores, AI feedback | `student_id`, `course_id`, `exam_id`, `rubric_id` |

The grading service's `student_id` should contain the User service's stable `User.id`, not a student number or email. Student/staff numbers and emails can change; the UUID is the durable relationship used for attempts and feedback.

This service intentionally does not duplicate Course, Exam, Question, Rubric, Attempt, or AI-feedback models. Those models stay in the service that owns their lifecycle.

### Three-service model contract

The models and database columns across the repositories use the following contract:

| Concept | Authoritative model | Referenced by | Contract |
|---|---|---|---|
| User identity | User `User.id` | Grading `Attempt.student_id`, JWT `sub` | UUID string, 36 characters; also valid under the shared 1–128 character external-ID format |
| Student/staff number | User `Student.student_number` / `Staff.staff_number` | User token `institutional_number` | 1–64 letters, numbers, `_`, `.`, or `-`; display/institutional identity, not a foreign key |
| Course | Grading `Course.id` | RAG `Rubric.course_id` and Qdrant metadata | Shared 1–128 character external ID |
| Exam/quiz | Grading `Exam.id` | RAG `Rubric.exam_id` and Qdrant metadata | Shared 1–128 character external ID |
| Rubric | RAG `Rubric.id` | Grading `Exam.rubric_id`, attempts, and grades | Shared 1–128 character external ID |
| Rubric document | RAG `StoredRubric.document_id` | Grading rubric metadata and Qdrant filtering | Generated UUID string |
| Rubric version | RAG rubric metadata | Grading attempts and grades | String, up to 64 characters in persistence |
| Question and attempt | Grading models | Grading responses and grades | Question IDs use the shared external-ID format; attempt IDs are generated UUID strings |

RAG and grading both use `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$` for shared resource identifiers. The User service exposes the same contract in `app/model/identifiers.py` and constrains generated user IDs/JWT subjects to UUID strings. This confirms that a User `id` can be passed unchanged as grading's `student_id`.

## User model

`Student` and `Staff` both inherit from the parent `User` Pydantic model:

```text
User
├── Student + student_number
└── Staff   + staff_number
```

Shared fields are:

- `id`: generated UUID and cross-service identity
- `email`: unique, normalized to lowercase
- `full_name`
- `role`: `student` or `staff`
- `active`: controls login and token acceptance
- `created_at` and `updated_at`

The PostgreSQL schema mirrors the inheritance model with a common `users` table and one-to-one `student_profiles` and `staff_profiles` tables. Emails are globally unique. Student numbers are unique among students, and staff numbers are unique among staff.

## Authentication and authorization

Passwords must contain 12–256 characters and are stored as salted `scrypt` hashes. A successful login returns a signed HS256 bearer token. The token contains:

- `sub`: stable `User.id`
- `role`: `student` or `staff`
- `email`
- `institutional_number`: the student or staff number
- `iss`, `aud`, `iat`, `exp`, and `jti`

Protected User service routes require `Authorization: Bearer <token>`. Each request verifies the signature, issuer, audience, issue/expiry time, and current database record. Deactivating a user therefore invalidates their existing tokens immediately.

Student self-registration is public. Staff registration additionally requires the bootstrap secret in `X-Staff-Registration-Key`. Rotate or disable that bootstrap path after provisioning production administrators. Staff can list users, read any profile, and activate/deactivate other accounts; students can only read their own profile.

## Run locally

```bash
cp .env.example .env
docker compose up --build -d
curl http://localhost:8002/health
```

Set strong, random values for `JWT_SECRET`, `STAFF_REGISTRATION_KEY`, and `POSTGRES_PASSWORD` before any non-development deployment. The JWT secret must be at least 32 bytes.

Useful commands:

```bash
make logs
make lint
make test
make down
```

## User flows

### 1. Register a student

The frontend collects the student's institutional number, email, full name, and password. The service creates the parent user and student profile in one database transaction; the response never contains the password hash.

```bash
curl -X POST http://localhost:8002/api/v1/auth/register/student \
  -H 'Content-Type: application/json' \
  -d '{
    "student_number":"S0001",
    "email":"student@example.edu",
    "full_name":"Student One",
    "password":"a-secure-student-password"
  }'
```

Duplicate emails or student numbers return `409`; invalid fields return `422`.

### 2. Register a staff member

An administrator supplies the server-managed staff bootstrap key in addition to the account fields.

```bash
curl -X POST http://localhost:8002/api/v1/auth/register/staff \
  -H 'Content-Type: application/json' \
  -H 'X-Staff-Registration-Key: <bootstrap-key>' \
  -d '{
    "staff_number":"E0001",
    "email":"staff@example.edu",
    "full_name":"Staff One",
    "password":"a-secure-staff-password"
  }'
```

A missing or incorrect bootstrap key returns `403`.

### 3. Log in

Students and staff use the same endpoint. Save the returned bearer token and the returned `user.id` in frontend session state.

```bash
curl -X POST http://localhost:8002/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{
    "email":"student@example.edu",
    "password":"a-secure-student-password"
  }'
```

Example response:

```json
{
  "access_token": "<signed-jwt>",
  "token_type": "bearer",
  "expires_in": 1800,
  "user": {
    "id": "60f1ec55-f74e-4924-a9ee-1d79f902f846",
    "email": "student@example.edu",
    "full_name": "Student One",
    "role": "student",
    "active": true,
    "student_number": "S0001",
    "created_at": "2026-08-18T00:00:00Z",
    "updated_at": "2026-08-18T00:00:00Z"
  }
}
```

Unknown credentials and inactive accounts return `401` without revealing whether the email exists.

### 4. Read the current profile

```bash
curl http://localhost:8002/api/v1/users/me \
  -H 'Authorization: Bearer <token>'
```

This is useful when restoring frontend session state after a reload. It also confirms that the account is still active.

### 5. Administer users as staff

List every user or filter by role:

```bash
curl 'http://localhost:8002/api/v1/users?role=student' \
  -H 'Authorization: Bearer <staff-token>'
```

Read a specific user:

```bash
curl http://localhost:8002/api/v1/users/<user-id> \
  -H 'Authorization: Bearer <staff-token>'
```

Deactivate an account:

```bash
curl -X PATCH http://localhost:8002/api/v1/users/<user-id>/status \
  -H 'Authorization: Bearer <staff-token>' \
  -H 'Content-Type: application/json' \
  -d '{"active":false}'
```

Staff cannot deactivate their own account. Deactivated users cannot log in or use tokens issued earlier.

### 6. Upload and manage a rubric as staff

1. Staff logs in to the User service and receives a token whose `role` is `staff`.
2. The frontend sends the rubric, `course_id`, and `exam_id` through a trusted backend or API gateway to the RAG upload endpoint.
3. RAG stores metadata in its PostgreSQL database, processes the file through its embedding container, and stores chunks in Qdrant.
4. Staff polls RAG processing status, inspects chunks, maps them to grading questions, and activates the rubric version on the exam.

The current RAG service still protects its API with `X-API-Key`; it does not yet validate User service bearer tokens. Keep the RAG API key in a trusted backend or API gateway, never in public browser code.

### 7. Submit answers for grading as a student

1. The student logs in and the frontend retains `user.id` from the login response.
2. The frontend creates or resumes an attempt for the tagged `exam_id`.
3. Until grading JWT validation is wired in, the frontend/backend identity adapter supplies `X-Student-ID: <user.id>` to the grading attempt routes.
4. The grading service stores answers under that attempt, reads the exam/rubric metadata from PostgreSQL, retrieves the mapped chunks directly from shared Qdrant, calls the external LLM, and stores the scores and feedback with the attempt.
5. The frontend retrieves the attempt result using the same stable user ID.

One-answer and multi-answer grading requests use the same attempt identity. The exam's `max_attempts` controls whether the student receives one or multiple attempts.

## API reference

| Method | Route | Access | Purpose |
|---|---|---|---|
| `GET` | `/health` | Public | Check PostgreSQL readiness |
| `POST` | `/api/v1/auth/register/student` | Public | Create a student account |
| `POST` | `/api/v1/auth/register/staff` | Bootstrap key | Create a staff account |
| `POST` | `/api/v1/auth/login` | Public | Verify credentials and issue a token |
| `GET` | `/api/v1/users/me` | Bearer token | Read the current profile |
| `GET` | `/api/v1/users` | Staff | List/filter users |
| `GET` | `/api/v1/users/{user_id}` | Self or staff | Read a user profile |
| `PATCH` | `/api/v1/users/{user_id}/status` | Staff | Activate/deactivate an account |

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `API_PORT` | `8002` | Host port used by Docker Compose |
| `DATABASE_URL` | local PostgreSQL URL | SQLAlchemy connection URL |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | development values | Compose database settings |
| `JWT_SECRET` | development-only value | HS256 signing secret, minimum 32 bytes |
| `JWT_ISSUER` | `user-service` | Required token issuer |
| `JWT_AUDIENCE` | `assessment-services` | Required downstream audience |
| `ACCESS_TOKEN_EXPIRY_MINUTES` | `30` | Token lifetime, 1–1440 minutes |
| `STAFF_REGISTRATION_KEY` | development-only value | Staff account bootstrap secret |
| `CORS_ORIGINS` | `*` | Comma-separated frontend origins |
| `LOG_LEVEL` | `INFO` | Application log level |

Downstream services that locally verify these HS256 tokens must use the same `JWT_SECRET`, `JWT_ISSUER`, and `JWT_AUDIENCE`. A production deployment can instead put verification in an API gateway; do not expose the shared JWT secret to the frontend.

## Testing and CI

```bash
python -m pip install -r requirements.txt
python -m pip install ruff==0.16.3
python -m ruff check app tests
python -m pytest -q
```

`.github/workflows/ci.yml` runs linting and tests for pull requests and non-`main` pushes. `.github/workflows/post-merge.yml` repeats both checks after a merge to `main`, then creates the next `v0.N` tag beginning with `v0.1`. The GitHub Container Registry job is included but commented out.

The repository follows the same operational layout as RAG and grading:

- `Dockerfile`, `.dockerignore`, `compose.yaml`, and `.env.example` for containerized local operation
- `requirements.txt` with pinned runtime and test dependencies; CI installs Ruff directly in YAML, so there is no separate CI requirements file
- `pytest.ini` plus controller, database, model, and service test directories
- `Makefile` targets for `up`, `down`, `logs`, `lint`, `test`, and `ci`
- matching pull-request and post-merge GitHub Actions workflows

## Current limitations

- RAG and grading do not yet validate this service's bearer tokens. RAG currently uses `X-API-Key`; grading uses `X-API-Key` plus the caller-provided `X-Student-ID`. Production integration must validate the JWT at a gateway or inside both services, derive student identity from `sub`, and enforce `staff` for administration routes.
- Tokens use a shared HS256 secret and there is no refresh-token or logout/revocation list. Account deactivation is checked by this service, but independently validating downstream services need a short token lifetime or an introspection/revocation strategy.
- Student self-registration is unrestricted beyond uniqueness and field validation. Institutional enrollment verification and email verification are not implemented.
- Database tables are created at startup with SQLAlchemy metadata. Use versioned migrations before evolving a production schema.
- Rate limiting, password reset, multi-factor authentication, and audit logging are not implemented.
