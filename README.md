# Backend

FastAPI service that hosts **identity** (student/staff accounts, auth) and
**catalog/grading** (courses, tests, questions, rubrics, few-shot examples,
attempts, LLM grading) in one app on one PostgreSQL database.

The service runs on port `8002` by default and exposes interactive
documentation at `http://localhost:8002/docs`.

## Two auth zones on one origin

| Zone | Routes | Mechanism |
|---|---|---|
| Identity | `GET /health`, `/api/v1/auth/*`, `/api/v1/users/*` | `Authorization: Bearer <jwt>` (public for register/login) |
| Catalog & grading | `/api/v1/courses/*`, `/api/v1/tests/*` | `X-API-Key` header, checked against `Settings.api_key` — skipped entirely if `API_KEY` is unset |

The catalog/grading zone's course/test-authoring routes still don't
validate the JWT — the `X-API-Key` alone gates them. Attempt routes
(`/tests/{id}/attempts*`) are the exception: they additionally require the
caller's own `Authorization: Bearer <jwt>`, and the attempt's owner is
always taken from that token's `sub` claim via the `current_user_id`
dependency, never from a caller-supplied id. Earlier this was an
unauthenticated `X-User-ID` header, which let anyone holding the API key
read or grade any user's attempts by simply naming a different id; that
gap is closed now that the identity itself is a verified token, not a
free-form header.

## User model

`Student` and `Staff` both inherit from the parent `User` Pydantic model:

```text
User
├── Student + student_number, role="student"
└── Staff   + staff_number,   role="instructor"
```

Shared fields: `id`, `email` (unique, lowercased), `full_name`, `role`,
`active`, `created_at`, `updated_at`.

`id` is a **plain sequential digit string**, not a UUID — students and
staff are numbered from separate ranges so the two can never collide:
students start at `1000001`, staff/instructors at `2000001`. It's assigned
by scanning existing ids for the role and taking the max + 1, done inside
the same transaction as the insert.

The PostgreSQL schema mirrors the inheritance model: a common `users` table
plus one-to-one `student_profiles`/`staff_profiles` tables. Emails are
globally unique; student numbers are unique among students; staff numbers
are unique among staff.

### DTOs

DTO definitions live in `app/dto/`.

| DTO | Purpose |
|---|---|
| `StudentRegistration` / `StaffRegistration` | Registration requests (`email`, `full_name`, `password`, and the role-specific number). |
| `LoginRequest` | Login credentials. |
| `UserResponse` | Discriminated union of `Student`/`Staff` by `role`. |
| `TokenResponse` | `access_token`, `token_type`, `expires_in`, `user`. |
| `UserStatusUpdate` | `active` — staff activate/deactivate request. |
| `TokenClaims` | `sub`, `role`, `email`, `institutional_number`, `iss`, `aud`, `iat`, `exp`, `jti`. |
| `HealthResponse` | `status`, `postgres`, `llm`, `model`. |
| `CourseCreate`, `TestCreate`, `QuestionCreate`, `RubricCreate`, `CriteriaCreate`, `ExampleCreate` | Catalog authoring requests. |
| `GradeAttemptRequest`, `FewShotGradeRequest` | Student-answer submission requests. |
| `AttemptGradeResponse` | Attempt + graded responses + score totals. |

## Authentication and authorization

Passwords must be 12–256 characters and are stored as salted `scrypt`
hashes. A successful login returns a signed HS256 bearer token containing
`sub` (the numeric user id), `role` (`student`/`instructor`), `email`,
`institutional_number`, and standard `iss`/`aud`/`iat`/`exp`/`jti` claims.

Every request to a protected identity-zone route re-verifies the
signature, issuer, audience, timestamps, and the current database record —
deactivating a user invalidates their existing tokens immediately.

Student self-registration is public. Staff registration additionally
requires the bootstrap secret in `X-Staff-Registration-Key`. Rotate or
disable that bootstrap path after provisioning production administrators.

## Catalog and grading domain

```text
Course
└── Test (max_attempts, questions)
    └── Question (max_score, score_increment, model_answer)
        ├── Rubric — criteria list, each with its own max score
        └── Examples — one good/average/poor exemplar answer + score each
```

A question is graded either by its **rubric** (LLM checks each criterion
independently, returns which are met) or by its **few-shot examples** (LLM
compares the answer holistically against the exemplars). If a question has
only one of the two attached, that one is used automatically. If it has
both, `PUT /tests/{id}/questions/{qid}/grading-method` must pick one before
an attempt can be created — otherwise attempt creation fails with 409.

Questions, rubrics, and examples can each be authored either via a single
JSON request or via CSV upload (`csv_import.py` parses with Python's `csv`
module — a real RFC 4180 parser, so answer text containing commas or line
breaks must be quoted; see [`demo/README.md`](../demo/README.md) for a
worked example). All three CSV formats join rows to questions by an
instructor-supplied `id` column, not by row position.

**Attempt flow:** `POST /tests/{id}/attempts` (with the student's bearer
token) creates an attempt owned by that token's user, failing fast if any
question's grading method is unresolved.
`POST /tests/{id}/attempts/{aid}/grade` saves the submitted answers and
kicks off grading in a background asyncio task per response, returning
immediately with the attempt in `grading` status — poll
`GET /tests/{id}/attempts/{aid}` for the graded result. A single answer
failing to grade (LLM error, invalid score) marks the whole attempt
`failed` with an error message, but responses already scored are kept.

`POST /tests/{id}/questions/{qid}/grade-fewshot` grades one ad-hoc answer
against a question's few-shot examples without touching the attempt
lifecycle at all — it exists purely to compare few-shot output against the
rubric-based path, and nothing is persisted.

### LLM grading

`LocalLLMClient` talks to any OpenAI-compatible chat-completions endpoint
(the default `.env.example` points at a local Ollama instance) using
`response_format: json_object`, then validates the returned JSON against
`CriteriaGradingResult` (rubric mode: score, feedback, per-criterion
met/unmet) or `FewShotGradingResult` (score, feedback). A criterion id the
LLM invents that isn't in the question's rubric, or a score outside
`[0, max_score]`, fails that response rather than being silently accepted.

## Run locally

```bash
cp .env.example .env
docker compose up --build -d
curl http://localhost:8002/health
```

Grading requires a reachable OpenAI-compatible LLM endpoint (`LLM_URL`) —
`/health` reports `llm` readiness by hitting that endpoint's `/models`
route, and grading itself will fail per-response if it's unreachable. The
default `.env.example` points at a local Ollama instance
(`http://host.docker.internal:11434/v1/chat/completions`); to use it,
install [Ollama](https://ollama.com), pull a model, and set `LLM_MODEL` to
match. For local development a small model such as `qwen3:0.6b` is enough
to exercise the grading flow end to end and starts up fast:

```bash
ollama pull qwen3:0.6b
# then set LLM_MODEL=qwen3:0.6b in .env
```

Swap in a larger model for grading quality closer to production.

Set strong, random values for `JWT_SECRET`, `STAFF_REGISTRATION_KEY`, and
`POSTGRES_PASSWORD` before any non-development deployment. The JWT secret
must be at least 32 bytes.

Useful commands:

```bash
make logs
make lint
make test
make down
```

## User flows

### 1. Register a student

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

A missing or incorrect bootstrap key returns `403`. The account is created
with `role: "instructor"`.

### 3. Log in

```bash
curl -X POST http://localhost:8002/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"student@example.edu","password":"a-secure-student-password"}'
```

Example response:

```json
{
  "access_token": "<signed-jwt>",
  "token_type": "bearer",
  "expires_in": 1800,
  "user": {
    "id": "1000001",
    "email": "student@example.edu",
    "full_name": "Student One",
    "role": "student",
    "active": true,
    "student_number": "S0001",
    "created_at": "2026-09-01T00:00:00Z",
    "updated_at": "2026-09-01T00:00:00Z"
  }
}
```

Unknown credentials and inactive accounts return `401` without revealing
whether the email exists.

### 4. Read the current profile

```bash
curl http://localhost:8002/api/v1/users/me -H 'Authorization: Bearer <token>'
```

### 5. Administer users as staff

```bash
curl 'http://localhost:8002/api/v1/users?role=student' -H 'Authorization: Bearer <staff-token>'
curl http://localhost:8002/api/v1/users/<user-id> -H 'Authorization: Bearer <staff-token>'
curl -X PATCH http://localhost:8002/api/v1/users/<user-id>/status \
  -H 'Authorization: Bearer <staff-token>' -H 'Content-Type: application/json' \
  -d '{"active":false}'
```

Staff cannot deactivate their own account.

### 6. Build a test as an instructor

```bash
curl -X POST http://localhost:8002/api/v1/courses \
  -H "X-API-Key: <key>" -H 'Content-Type: application/json' \
  -d '{"course_code":"CS101","course_name":"Intro to CS"}'

curl -X POST http://localhost:8002/api/v1/courses/<course_id>/tests/csv \
  -H "X-API-Key: <key>" \
  -F "file=@questions.csv;type=text/csv" -F "test_name=Quiz 1" -F "max_attempts=1"

curl -X POST http://localhost:8002/api/v1/tests/<test_id>/criteria/csv \
  -H "X-API-Key: <key>" -F "file=@criteria.csv;type=text/csv"

curl -X POST http://localhost:8002/api/v1/tests/<test_id>/examples/csv \
  -H "X-API-Key: <key>" -F "file=@examples.csv;type=text/csv"
```

See [`demo/README.md`](../demo/README.md) for ready-made sample CSVs and
the full end-to-end walkthrough, including the frontend flow.

### 7. Submit and grade an attempt as a student

The student's own bearer token from login (step 3) identifies whose
attempt this is — there's no separate id to pass.

```bash
curl -X POST http://localhost:8002/api/v1/tests/<test_id>/attempts \
  -H "X-API-Key: <key>" -H "Authorization: Bearer <student-token>"

curl -X POST http://localhost:8002/api/v1/tests/<test_id>/attempts/<attempt_id>/grade \
  -H "X-API-Key: <key>" -H "Authorization: Bearer <student-token>" -H 'Content-Type: application/json' \
  -d '{"responses":[{"question_id":"<question_id>","answer":"..."}]}'

curl http://localhost:8002/api/v1/tests/<test_id>/attempts/<attempt_id> \
  -H "X-API-Key: <key>" -H "Authorization: Bearer <student-token>"
```

Grading runs in the background per response — poll the last endpoint until
`attempt.status` is `graded` or `failed`.

## API reference

| Method | Route | Access | Purpose |
|---|---|---|---|
| `GET` | `/health` | Public | Postgres + LLM readiness |
| `POST` | `/api/v1/auth/register/student` | Public | Create a student account |
| `POST` | `/api/v1/auth/register/staff` | Bootstrap key | Create a staff account |
| `POST` | `/api/v1/auth/login` | Public | Verify credentials, issue a token |
| `GET` | `/api/v1/users/me` | Bearer token | Read the current profile |
| `GET` | `/api/v1/users` | Staff | List/filter users |
| `GET` | `/api/v1/users/{user_id}` | Self or staff | Read a user profile |
| `PATCH` | `/api/v1/users/{user_id}/status` | Staff | Activate/deactivate an account |
| `POST` | `/api/v1/courses` | API key | Create a course |
| `GET` | `/api/v1/courses` | API key | List courses |
| `POST` | `/api/v1/courses/{course_id}/tests` | API key | Create a test (JSON questions) |
| `POST` | `/api/v1/courses/{course_id}/tests/csv` | API key | Create a test from a questions CSV |
| `GET` | `/api/v1/courses/{course_id}/tests` | API key | List a course's tests |
| `GET` | `/api/v1/tests/{test_id}` | API key | Read a test and its questions |
| `PUT` | `/api/v1/tests/{test_id}/questions/{question_id}/rubric` | API key | Set a question's rubric |
| `PUT` | `/api/v1/tests/{test_id}/questions/{question_id}/grading-method` | API key | Force rubric vs. few-shot when both are attached |
| `POST` | `/api/v1/tests/{test_id}/criteria/csv` | API key | Bulk-attach rubrics from CSV |
| `POST` | `/api/v1/tests/{test_id}/examples/csv` | API key | Bulk-attach few-shot examples from CSV |
| `POST` | `/api/v1/tests/{test_id}/questions/{question_id}/grade-fewshot` | API key | Ad-hoc few-shot grading, not persisted |
| `POST` | `/api/v1/tests/{test_id}/attempts` | API key + bearer token | Start an attempt for the token's user |
| `GET` | `/api/v1/tests/{test_id}/attempts` | API key + bearer token | List the token's user's attempts |
| `POST` | `/api/v1/tests/{test_id}/attempts/{attempt_id}/grade` | API key + bearer token | Submit answers, start background grading |
| `GET` | `/api/v1/tests/{test_id}/attempts/{attempt_id}` | API key + bearer token | Read attempt status and graded responses |

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `API_PORT` | `8002` | Host port used by Docker Compose |
| `DATABASE_URL` | local PostgreSQL URL | SQLAlchemy connection URL for this service's own database |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | development values | Compose database settings |
| `API_KEY` | unset | `X-API-Key` required on `/courses` and `/tests` routes; unset disables the check entirely |
| `JWT_SECRET` | development-only value | HS256 signing secret, minimum 32 bytes |
| `JWT_ISSUER` | `user-service` | Required token issuer |
| `JWT_AUDIENCE` | `assessment-services` | Required downstream audience |
| `ACCESS_TOKEN_EXPIRY_MINUTES` | `30` | Token lifetime, 1–1440 minutes |
| `STAFF_REGISTRATION_KEY` | development-only value | Staff account bootstrap secret |
| `LLM_URL` | local Ollama chat-completions URL | OpenAI-compatible grading endpoint |
| `LLM_MODEL` | `local-model` | Model name sent in grading requests |
| `LLM_API_KEY` | unset | Bearer token for the LLM endpoint, if it requires one |
| `LLM_TIMEOUT_SECONDS` / `LLM_MAX_RETRIES` / `LLM_TEMPERATURE` / `LLM_MAX_TOKENS` | `120` / `1` / `0` / `2048` | LLM request tuning |
| `MAX_ANSWER_CHARACTERS` | `50000` | Longest student answer accepted before grading rejects it |
| `CORS_ORIGINS` | `*` | Comma-separated frontend origins |
| `LOG_LEVEL` | `INFO` | Application log level |

Downstream services that locally verify these HS256 tokens must use the
same `JWT_SECRET`, `JWT_ISSUER`, and `JWT_AUDIENCE`. A production
deployment can instead put verification in an API gateway; do not expose
the shared JWT secret to the frontend.

## Testing and CI

```bash
python -m pip install -r requirements.txt
python -m pip install ruff==0.16.3
python -m ruff check app tests
python -m pytest -q
```

`.github/workflows/ci.yml` runs linting and tests for pull requests and
pushes to `main`. `.github/workflows/post-merge.yml` repeats both checks
after a merge to `main`, then creates the next `v0.N` tag beginning with
`v0.1`. The GitHub Container Registry publish job is included but
commented out.

Test layout under `tests/` mirrors `app/`: `controller/`, `db/`, `model/`,
and `service/` directories, plus `conftest.py` for shared fixtures.

## Current limitations

- The catalog/grading zone's course/test-authoring routes (`X-API-Key`
  only) still don't verify the caller's JWT or role — anything holding the
  API key can create courses/tests and attach rubrics. Attempt routes are
  the exception and now require the caller's own bearer token. It's
  designed to sit behind a trusted backend or gateway, not to be exposed
  directly.
- Tokens use a shared HS256 secret; there is no refresh token or
  logout/revocation list beyond checking `active` on every request.
- Student self-registration is unrestricted beyond uniqueness and field
  validation — no institutional enrollment or email verification.
- Database tables are created at startup with SQLAlchemy metadata
  (`create_all`); there are no versioned migrations, so schema changes
  require manually recreating affected tables in any pre-existing database.
- Rate limiting, password reset, multi-factor authentication, and audit
  logging are not implemented.
