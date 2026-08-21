.PHONY: install test integration integration-up integration-down lint check-peek all

all: lint test

install:
	./install.sh

test:
	./venv/bin/python -m pytest -v

integration:
	./venv/bin/python -m pytest -m integration -v

integration-up:
	docker compose -f tests/integration/docker-compose.yml up -d
	@echo "waiting for dovecot..."
	@for i in $$(seq 1 30); do \
	    nc -z localhost 10143 && nc -z localhost 10144 && exit 0; \
	    sleep 1; \
	done; echo "dovecot did not come up" >&2; exit 1

integration-down:
	docker compose -f tests/integration/docker-compose.yml down -v

lint: check-peek
	./venv/bin/python -m ruff check lib tests

# A BODY[ fetch without PEEK sets \Seen. Refuse to build if one appears.
check-peek:
	@! grep -rn "BODY\[" lib/ --include=*.py \
	  | grep -v "BODY.PEEK\[" \
	  | grep -v "# response-key" \
	  || (echo "FATAL: non-PEEK BODY[ fetch found above" && exit 1)
