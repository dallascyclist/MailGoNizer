.PHONY: install test integration lint check-peek all

all: lint test

install:
	./install.sh

test:
	./venv/bin/python -m pytest -v

integration:
	./venv/bin/python -m pytest -m integration -v

lint: check-peek
	./venv/bin/python -m ruff check lib tests

# A BODY[ fetch without PEEK sets \Seen. Refuse to build if one appears.
check-peek:
	@! grep -rn "BODY\[" lib/ --include=*.py \
	  | grep -v "BODY.PEEK\[" \
	  | grep -v "# response-key" \
	  || (echo "FATAL: non-PEEK BODY[ fetch found above" && exit 1)
