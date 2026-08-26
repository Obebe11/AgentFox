.PHONY: verify test bench fingerprint live clean

# Стандарт верификации: только реальные рабочие проверки (VERIFICATION.md)
# Каждая оптимизация должна пройти оригинал vs форк сравнение

verify: test
	python -m tools.verify_optimization
	@echo "--- VERIFICATION_REPORT.md: $$(grep -o '[0-9]*/[0-9]* PASS' VERIFICATION_REPORT.md | head -1) ---"

test:
	pytest -q

bench:
	python -m tools.verify_optimization --json report.json
	cat VERIFICATION_REPORT.md

fingerprint:
	python -m tools.fingerprint_check --live --profiles 2

live: bench fingerprint

clean:
	rm -rf profiles/metrics.db report.json VERIFICATION_REPORT.md.bak
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null; true

install:
	pip install -e ".[all]"
	python -m camoufox fetch || true

docker:
	docker build -f docker/Dockerfile -t agentfox:runtime .

# Одна кнопка для юзера (форк ставится автоматом из Obebe11/camoufox)
install-one:
	pip install git+https://github.com/Obebe11/AgentFox.git#[all]
	python -m camoufox fetch
