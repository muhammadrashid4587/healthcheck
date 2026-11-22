test:
	python3 -m unittest discover -s tests -v

run:
	python3 -m healthcheck.cli --config config.example.json

once:
	python3 -m healthcheck.cli --config config.example.json --once

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

.PHONY: test run once clean
