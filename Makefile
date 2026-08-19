.PHONY: up down logs lint test ci

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f api

lint:
	python -m ruff check app tests

test:
	python -m pytest -q

ci: lint test
