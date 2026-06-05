# Unified CubeAPM data generator.
# Usage: edit config.yaml, then `make up`.  `make down` to stop.
SHELL   := /bin/bash
COMPOSE := docker compose -f docker-compose.generated.yml
PYIMG   := python:3.12-slim

.PHONY: up down logs ps generate regen certs clean

## generate: render compose + nginx + targets/streams from config.yaml
## (runs in a container so the host needs only Docker — no Python/PyYAML).
generate:
	docker run --rm -v "$(CURDIR)":/work -w /work $(PYIMG) \
		sh -c "pip install -q pyyaml && python generate.py"

## certs: self-signed cert for the New Relic TLS gateway (no-op if it exists)
certs:
	@mkdir -p certs
	@if [ ! -f certs/gateway.crt ]; then \
	  echo "Generating self-signed tenant-gateway cert..."; \
	  openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
	    -keyout certs/gateway.key -out certs/gateway.crt \
	    -subj "/CN=tenant-gateway" \
	    -addext "subjectAltName=DNS:tenant-gateway,DNS:host.docker.internal,DNS:localhost"; \
	else echo "certs/gateway.crt already present."; fi

## up: generate, build and start the whole stack in the background
up: generate certs
	$(COMPOSE) up --build -d
	@echo ""
	@echo "Stack is up. Reminder: the org slugs in config.yaml must already exist"
	@echo "in CubeAPM, or data lands in the default org. Tail logs: make logs"

## down: stop and remove the stack (keeps the mysql volume)
down:
	$(COMPOSE) down

## clean: down + remove the mysql volume
clean:
	$(COMPOSE) down -v

## logs / ps
logs:
	$(COMPOSE) logs -f --tail=50

ps:
	$(COMPOSE) ps

## regen: re-render only (after editing config.yaml); then `make up` to apply
regen: generate
	@echo "Regenerated. Run 'make up' to apply."
