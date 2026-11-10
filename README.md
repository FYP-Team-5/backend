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
| Catalog & grading | `/api/v1/courses/*`, `/api/v1/tests/*` | Optional `X-API-Key` gate plus role-checked bearer tokens for test authoring, instructor actions, and student attempts |

Course listing uses the API-key gate. Reading tests also requires a verified
bearer token; student views omit model answers, rubrics, and grading examples.
Course and test creation,
rubric/example updates, grading-method selection, ad-hoc grading, submission
review, grade editing, deadline updates, and manual test closure also require a
verified instructor bearer token. Student attempt routes require a verified
student bearer token and always derive ownership from its user id.

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

**Attempt flow:** `POST /tests/{id}/attempts` creates an attempt for the
student's bearer token, subject to the attempt limit, grading readiness, and
test availability. `POST /tests/{id}/attempts/{aid}/grade` atomically saves the
answers and a persistent grading job, returning immediately. Students can
leave, list their attempts, and reopen the same result URL while grading runs.
Submitted answers are immutable. Repeating the same accepted request is safe.

The worker uses PostgreSQL row locks, renewable leases, and lease tokens.
It saves each question's grade separately, retries transient failures with
bounded exponential backoff, and recovers pending or expired work after a
restart. Already graded questions are skipped, and stale workers cannot
publish results. The accepted test/rubric/example snapshot is retained so
catalog changes do not change an accepted submission's grading configuration.
An interrupted LLM request may run again; persisted results are applied once.
`POST /tests/{id}/attempts/{aid}/retry-grading` retries a failed job using saved
answers, including after test closure.

**Availability:** New tests require a future, timezone-aware `ends_at` through
both JSON and CSV creation. Existing tests may have no deadline. Availability
is computed on every server check: a test closes at or after its deadline or
when an instructor ends it. Closure and submission acceptance share a
transactional test lock. Closure blocks new attempts and answers while
accepted grading, result viewing, and instructor grade editing remain
available. Unsubmitted browser answers are not automatically submitted.

**Instructor review:** The submissions endpoint lists every registered
student, including those without a submission, with the latest submitted
attempt and latest graded attempt separately. Instructors can edit per-question
scores and feedback only on the latest graded submission. Writes validate
score bounds/increments and an attempt revision; a stale edit returns 409.
Original automated results and an audit of old/new values, editor, and time
are retained. Totals and percentages reflect saved overrides immediately.

`POST /tests/{id}/questions/{qid}/grade-fewshot` grades one ad-hoc answer
against a question's few-shot examples without touching the attempt
lifecycle at all — it exists purely to compare few-shot output against the
rubric-based path, and nothing is persisted.

### LLM grading

`LocalLLMClient` uses the configured OpenAI-compatible chat-completions endpoint.
Rubric grading calls two different model aliases on that endpoint:

1. `LLM_MODEL` returns a JSON array of integer labels such as `[1, 0]`, in rubric
   order: `1` means met and `0` means not met. Booleans, other values and arrays
   of the wrong length fail validation. The backend calculates the score from
   the saved criterion weights and labels.
2. `LLM_FEEDBACK_MODEL` receives the question, reference answer, student answer,
   every criterion marked `[MET]` or `[NOT MET]`, and the number met. It returns
   plain-text tutor feedback acknowledging correct work and explaining missing
   or incorrect ideas. The prompt matches `feedback/qlora_feedback_qwen3.5-9B.ipynb`.
   Feedback cannot change the saved labels or score.

The grading decisions are checkpointed before feedback generation, so a feedback
failure or restart retries feedback without rerunning successful grading. A
response becomes complete only when its score, feedback and criterion records
are saved together. Both model names are saved with accepted jobs and retained
through retries and partial submissions. Existing API response shapes, including
criterion-id/boolean `criteria_met` records, are unchanged.

The default `LLM_GRADING_CONTRACT=criteria_array_v1` matches the grading LoRA.
`grading_json_v1` remains available for older models/jobs; it requires exactly one
decision per criterion and uses only those decisions to calculate the score.
Few-shot grading retains its existing JSON score/feedback contract and model.
Both models must be available on `LLM_URL`; their aliases must differ. The
deployment serving wrapper loads the grading and feedback adapters into the same
Qwen3.5-9B process. For a private feedback repository, configure Hugging Face read
access on the serving host or set `FEEDBACK_LORA_PATH` to a local adapter directory.

## Run locally

### Prerequisites

- Docker and Docker Compose.
- [Ollama](https://ollama.com), installed and running locally. Grading
  requires a reachable OpenAI-compatible LLM endpoint (`LLM_URL`) —
  `/health` reports `llm` readiness by hitting that endpoint's `/models`
  route, and grading itself will fail per-response if it's unreachable. The
  default `.env.example` points at Ollama's default address as seen from
  inside the container (`http://host.docker.internal:11434/v1/chat/completions`),
  so no separate container is needed — just the Ollama app/service running
  on the host.

Pull a model and point `LLM_MODEL` at it. For local development a small
model such as `qwen3:0.6b` is enough to exercise the grading flow end to
end and starts up fast:

```bash
ollama pull qwen3:0.6b
# then set LLM_MODEL=qwen3:0.6b in .env
```

Swap in a larger model for grading quality closer to production.

### Start the stack

```bash
cp .env.example .env
docker compose up --build -d
curl http://localhost:8002/health
```

Check `/health`'s `llm` field — if it's `false`, Ollama isn't reachable at
`LLM_URL` yet.

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

### Accounts created on startup

The backend creates these accounts automatically when `SEED_DEMO_USERS=true`
(the default). Use the email as the username on the frontend login page or with
`POST /api/v1/auth/login`.

| Role | Username (email) | Password |
|---|---|---|
| Student | `student@example.com` | `Password123!` |
| Student | `student2@example.com` | `Password123!` |
| Instructor | `instructor@example.com` | `Password123!` |

When demo seeding is enabled, the backend also creates the `DEMO101` course
with two identical tests and one completed graded attempt for each demo student
in each test. The first student receives 10/10 and the second receives 8/10,
which provides data for student history and instructor submission views.

Passwords are stored as salted hashes. Startup creates missing accounts and
preserves existing accounts, including their passwords, roles, profiles, and
inactive status. Restarting the backend does not reset credentials or create
duplicates. If either email already exists, use that account's existing password.

These are public demo credentials. Set `SEED_DEMO_USERS=false` in the backend
environment for deployments that should not create them. Disabling seeding
preserves accounts already created; an instructor can deactivate them through
the user administration API.

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
  -H "Authorization: Bearer <instructor-token>" \
  -d '{"course_code":"CS101","course_name":"Intro to CS"}'

curl -X POST http://localhost:8002/api/v1/courses/<course_id>/tests/csv \
  -H "X-API-Key: <key>" \
  -H "Authorization: Bearer <instructor-token>" \
  -F "file=@questions.csv;type=text/csv" -F "test_name=Quiz 1" -F "max_attempts=1" \
  -F "ends_at=<future-ISO-8601-timestamp-with-timezone>"

curl -X POST http://localhost:8002/api/v1/tests/<test_id>/criteria/csv \
  -H "X-API-Key: <key>" -H "Authorization: Bearer <instructor-token>" -F "file=@criteria.csv;type=text/csv"

curl -X POST http://localhost:8002/api/v1/tests/<test_id>/examples/csv \
  -H "X-API-Key: <key>" -H "Authorization: Bearer <instructor-token>" -F "file=@examples.csv;type=text/csv"
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

Grading runs in a persistent background job per submission — poll the last endpoint until
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
| `POST` | `/api/v1/courses` | API key + instructor token | Create a course |
| `GET` | `/api/v1/courses` | API key | List courses |
| `POST` | `/api/v1/courses/{course_id}/tests` | API key + instructor token | Create a test (JSON questions) |
| `POST` | `/api/v1/courses/{course_id}/tests/csv` | API key + instructor token | Create a test from a questions CSV |
| `GET` | `/api/v1/courses/{course_id}/tests` | API key + bearer token | List tests with role-appropriate question details |
| `GET` | `/api/v1/tests/{test_id}` | API key + bearer token | Read a test with role-appropriate question details |
| `PUT` | `/api/v1/tests/{test_id}/questions/{question_id}/rubric` | API key + instructor token | Set a question's rubric |
| `PUT` | `/api/v1/tests/{test_id}/questions/{question_id}/grading-method` | API key + instructor token | Force rubric vs. few-shot when both are attached |
| `POST` | `/api/v1/tests/{test_id}/criteria/csv` | API key + instructor token | Bulk-attach rubrics from CSV |
| `POST` | `/api/v1/tests/{test_id}/examples/csv` | API key + instructor token | Bulk-attach few-shot examples from CSV |
| `POST` | `/api/v1/tests/{test_id}/questions/{question_id}/grade-fewshot` | API key + instructor token | Ad-hoc few-shot grading, not persisted |
| `POST` | `/api/v1/tests/{test_id}/attempts` | API key + bearer token | Start an attempt for the token's user |
| `GET` | `/api/v1/tests/{test_id}/attempts` | API key + bearer token | List the token's user's attempts |
| `POST` | `/api/v1/tests/{test_id}/attempts/{attempt_id}/grade` | API key + bearer token | Submit answers, start background grading |
| `GET` | `/api/v1/tests/{test_id}/attempts/{attempt_id}` | API key + bearer token | Read attempt status and graded responses |
| `GET` | `/api/v1/tests/{test_id}/submissions` | API key + instructor token | Latest submissions for all registered students |
| `GET` | `/api/v1/tests/{test_id}/submissions/{attempt_id}` | API key + instructor token | Review a submitted attempt |
| `PATCH` | `/api/v1/tests/{test_id}/submissions/{attempt_id}/grades` | API key + instructor token | Update scores/feedback with `revision` and response ids |
| `PATCH` | `/api/v1/tests/{test_id}/deadline` | API key + instructor token | Update an open test's `ends_at` |
| `POST` | `/api/v1/tests/{test_id}/end` | API key + instructor token | End a test idempotently |
| `POST` | `/api/v1/tests/{test_id}/attempts/{attempt_id}/retry-grading` | API key + student token | Retry saved answers on a failed job |

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
| `SEED_DEMO_USERS` | `true` | Create the documented demo student and instructor accounts if missing at startup |
| `LLM_URL` | local Ollama chat-completions URL | OpenAI-compatible grading endpoint |
| `LLM_MODEL` | `local-model` | Model name sent in grading requests |
| `LLM_GRADING_CONTRACT` | `criteria_array_v1` | Rubric output contract; `grading_json_v1` supports older criterion-object models |
| `LLM_FEEDBACK_MODEL` | `qwen35-9b-feedback-qlora` | Separate model alias generating plain-text feedback from met/unmet decisions |
| `LLM_API_KEY` | unset | Bearer token for the LLM endpoint, if it requires one |
| `LLM_TIMEOUT_SECONDS` / `LLM_MAX_RETRIES` / `LLM_TEMPERATURE` / `LLM_MAX_TOKENS` | `120` / `1` / `0` / `2048` | LLM request tuning |
| `GRADING_POLL_SECONDS` | `1` | Pending/expired job scan interval |
| `GRADING_LEASE_SECONDS` | `60` | Worker lease duration, renewed every third of this interval |
| `GRADING_MAX_RETRIES` | `3` | Automatic retries after the initial claim; expired leases also count |
| `GRADING_RETRY_SECONDS` | `5` | Base delay for exponential retry backoff |
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

- Course listing uses only the API-key gate. Test reads require a verified JWT;
  course/test authoring and instructor review/mutations verify the instructor
  role. There is no course enrollment or instructor ownership model; instructor
  access is global and submission rosters include all registered students.
- Tokens use a shared HS256 secret; there is no refresh token or
  logout/revocation list beyond checking `active` on every request.
- Student self-registration is unrestricted beyond uniqueness and field
  validation — no institutional enrollment or email verification.
- Database tables are created at startup with SQLAlchemy metadata
  (`create_all`); there are no versioned migrations, so schema changes
  require manually recreating affected tables in any pre-existing database.
- Rate limiting, password reset, multi-factor authentication, and general
  account audit logging are not implemented. Instructor grade-change auditing is implemented.


## PostgreSQL concurrency verification

The normal test suite uses fresh SQLite databases. To exercise real row locks,
set `TEST_DATABASE_URL` to a disposable PostgreSQL database and run:

```bash
TEST_DATABASE_URL=postgresql+psycopg://... python -m pytest tests/db/test_grading_postgres.py -q
```

These tests create a random schema per test and remove it afterward. They cover
worker claims, duplicate submissions, attempt limits, closure/acceptance races,
and grade-edit/completion races. The database role needs schema-creation rights.
No migration files or database reset commands are added by this change; update
schema definitions and initialize a fresh database through `create_all()`.


## SeaweedFS and versioned training CSVs

The backend Compose stack includes `chrislusf/seaweedfs:4.52`, running the
single-process `mini` service with S3, persistent data and filer metadata under
`/data`, and a named `seaweedfs_data` volume. Its S3 endpoint is internal to the
Compose network at `http://seaweedfs:8333`; storage credentials stay on the backend.
The pinned image and flags follow the
[SeaweedFS 4.52 implementation](https://github.com/seaweedfs/seaweedfs/blob/4.52/weed/command/mini.go).

Configure `DATASET_S3_ACCESS_KEY`, `DATASET_S3_SECRET_KEY`, and
`DATASET_S3_BUCKET` in `.env`, then start the backend stack as usual. The API
starts independently of storage availability. `/health` reports cached
`seaweedfs` readiness separately; unavailable dataset storage does not make
otherwise healthy identity/grading services fail their health check.

Instructors open **Training dataset** from the dashboard to upload a CSV, browse
version history, download the latest or historical CSV, or retry a failed
publication. Each ready version has an immutable object key and SHA-256 checksum.
A new upload replaces the seed examples while retaining automatically collected
test rows. Re-uploading an exact previously published snapshot recognizes its
seed snapshot and avoids importing its generated rows a second time.

The CSV contract matches
`qlora/Datasets/training_data_moh_eng_rubrics.csv` in the sibling QLoRA checkout:

```csv
id,question,desired_answer,student_answer,criteria[],criteria_score[],criteria_max_score[],student_score
```

Array cells contain JSON arrays quoted by the CSV writer. UTF-8 BOMs, quoted
commas, multiline fields, and repeated question ids are supported. Scores must
be finite, within criterion maxima, and add up to `student_score`. Blank student
answers are accepted with zero scores, matching the reference dataset. Dataset
files remain CSV; provenance, source revisions, and job metadata live in
PostgreSQL. Student names, emails, and institutional numbers are not added to CSVs.

### Automatic collection

The dataset worker periodically scans closed tests, including deadlines that
expire without any page request. It selects each student's latest submitted
attempt and waits for accepted grading to settle. It exports complete, graded
latest attempts; failed/latest-pending submissions never cause an older graded
attempt to be used instead. Failed grading can later be retried and included.

Each eligible question contributes one row using its accepted rubric snapshot.
Automatically generated rows have stable source identities in a separate ledger,
so repeated scans or worker recovery do not append duplicate rows. Instructor
corrections after closure create a new CSV version with corrected rows while
historical versions remain unchanged. Distinct students with identical answers
remain distinct examples.

Criterion scores must match the saved aggregate grade. Submission review now
allows per-criterion corrections, including partial credit. Aggregate-only
changes that disagree with criterion labels, incomplete labels, and questions
without an accepted rubric are reported as excluded rows with actionable reasons.
They are collected automatically after valid labels are saved. The dataset
workflow prepares training data; it does not start model training.

### Publication and recovery

Version reservations, seed rows, the generated-row ledger, source provenance,
leases, retry counters, and errors are durable in PostgreSQL. CSV bytes are
derived from that saved state for each transfer rather than duplicated as a
database blob. The worker transfers and
verifies immutable objects outside database transactions, then checks its lease,
active parent, and source revisions before activating a ready version. Source
changes during transfer cause a rebuild at a distinct checksum-based object key;
ready objects are never overwritten. Superseded staging objects are cleaned up
after publication. Downloads verify actual stored bytes before streaming a CSV.

Only one publisher can own the dataset lease. Pending/expired work is recovered
on startup, transient failures receive bounded backoff, and explicit retry
reserves a new version without rewriting failed history. If an automatic
reservation becomes redundant after another publication, its status is
`unchanged` and it does not create another ready CSV. Storage failures leave the
current ready version available and do not cancel quiz grading or test closure.

All routes below require an instructor bearer token and the API key if configured:

| Method | Route | Purpose |
|---|---|---|
| GET | `/api/v1/training-dataset/status` | Check storage readiness |
| POST | `/api/v1/training-dataset/versions` | Upload multipart `.csv`; optional `Idempotency-Key`; returns 202 |
| GET | `/api/v1/training-dataset/versions` | Read version history and active version |
| GET | `/api/v1/training-dataset/versions/{id}` | Read version metadata/status |
| GET | `/api/v1/training-dataset/versions/{id}/download` | Download a ready historical CSV |
| GET | `/api/v1/training-dataset/download` | Download the latest ready CSV |
| POST | `/api/v1/training-dataset/versions/{id}/retry` | Retry failed publication |
| GET | `/api/v1/tests/{id}/training-export` | Read export progress, exclusions, errors, and version |
| POST | `/api/v1/tests/{id}/training-export/retry` | Retry the test's failed publication |

Grade edits additionally accept `criterion_scores` on each changed response:

```json
{"revision": 2, "responses": [{"response_id": "response-id", "score": 2,
  "feedback": "Reviewed", "criterion_scores": [{"criteria_id": "criterion-id", "score": 2}]}]}
```

Provide exactly one score per accepted rubric criterion when correcting criterion
labels. Omit `criterion_scores` for feedback/aggregate-only edits. Totals must
still satisfy the question's score bounds and increment.

| Variable | Default | Purpose |
|---|---|---|
| `DATASET_S3_ENDPOINT` | `http://localhost:8333` | Host-run S3 endpoint; Compose supplies the internal endpoint |
| `DATASET_S3_BUCKET` | `llm-training` | Dataset bucket |
| `DATASET_S3_ACCESS_KEY` | Development value | Backend-only S3 access key |
| `DATASET_S3_SECRET_KEY` | Development value | Backend-only S3 secret |
| `DATASET_S3_REGION` | `us-east-1` | S3 signing region |
| `DATASET_UPLOAD_MAX_BYTES` | `52428800` | Maximum uploaded CSV size |
| `DATASET_POLL_SECONDS` | `5` | Closed-test reconciliation/pending-job interval |
| `DATASET_LEASE_SECONDS` | `120` | Publication lease, renewed every third of this interval |
| `DATASET_MAX_RETRIES` | `3` | Automatic retries after the initial claim |
| `DATASET_RETRY_SECONDS` | `5` | Base exponential retry delay |

### Dataset integration tests

The regular suite uses an in-memory storage fake. PostgreSQL and SeaweedFS tests
are opt-in and target disposable test services:

```bash
TEST_DATABASE_URL='postgresql+psycopg://...' \
DATASET_TEST_S3_ENDPOINT=http://127.0.0.1:18333 \
DATASET_TEST_S3_ACCESS_KEY=... DATASET_TEST_S3_SECRET_KEY=... \
python -m pytest -q
```

PostgreSQL tests use random schemas. SeaweedFS tests use random buckets and clean
up their own objects. If the QLoRA checkout is present, round-trip tests use all
3,778 reference rows; otherwise they use the committed small reference fixture.
No migration or database reset commands are added. Initialize a fresh schema
with the existing `create_all()` approach.

## Nightly model candidates and explicit deployment

The deployment workflow publishes evaluated adapters to `candidate-<run-id>` branches in `SmuFypTeam5/GradingQlora`, then calls `POST /api/v1/training-runs`. This machine endpoint requires `X-Training-Token` matching `TRAINING_CALLBACK_TOKEN`; it does not accept an instructor JWT in place of that credential. Run ids are immutable and callback replays are idempotent. The manifest records the exact HF commit, dataset identity, effective configuration, candidate metrics and baseline metrics.

Verified instructors use `GET /api/v1/training-runs` and `POST /api/v1/training-runs/{run_id}/deploy` with `{"revision": <current revision>}`. Deployment returns 202 and runs through a persisted PostgreSQL worker. It validates the pinned adapter's files/base model/rank, loads a separate vLLM alias and checks grading responses before atomically updating the active pointer. Failed or stale work cannot replace the active model; the page shows status and a retry action. Choosing a previous candidate explicitly supports rollback. This repository has an instructor role; no separate admin role is introduced.

The HF repository is public; candidate downloads are anonymous. Only the publisher needs an upload token, stored in SSM. Set the backend `TRAINING_HF_REPO`, `TRAINING_CALLBACK_TOKEN` and shared `MODEL_ADAPTER_DIRECTORY` as described in `.env.example`. The host's updated vLLM wrapper must opt into runtime LoRA loading and the filesystem resolver, with the same adapter directory path available inside the API container. Keep vLLM administration private. See the deployment repository's `autotraining/README.md` for credential names and serving configuration; these changes do not replace the serving EC2 instance.

The binary rubric contract is supported explicitly: criterion labels must be exactly aligned integer 0/1 values and map to saved weights/criterion ids. A separate feedback model explains both met and unmet criteria using the feedback adapter's training prompt. Few-shot grading uses the original configured model. Accepted jobs save the rubric model name/contract/HF revision and feedback model name, retaining them through retries and partial submissions. Missing approved grading adapter cache files can be restored from that exact HF revision. Feedback adapter deployment is configured separately from the grading candidate registry.
