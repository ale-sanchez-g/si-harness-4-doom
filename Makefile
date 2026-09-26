COMPOSE ?= docker compose
GPU := -f docker-compose.yml -f docker-compose.gpu.yml
HOST_OLLAMA := -f docker-compose.yml -f docker-compose.host-ollama.yml
# OBSERVE=1 sends the harness traces to Phoenix (start it with `make observe` or `make phoenix`)
TRACE_ENV := $(if $(OBSERVE),-e OTEL_EXPORTER_OTLP_ENDPOINT=http://phoenix:6006,)

.PHONY: up gpu host-ollama observe phoenix down logs play scripted bench eval compare report check prompt pull test

up:            ## Start Doom + Ollama + harness (CPU) and follow the harness
	$(COMPOSE) up --build -d doom ollama
	$(COMPOSE) up --build harness

gpu:           ## Same, with an NVIDIA GPU for Ollama
	$(COMPOSE) $(GPU) up --build -d doom ollama
	$(COMPOSE) $(GPU) up --build harness

host-ollama:   ## Use the Ollama running on this machine (e.g. macOS app)
	$(COMPOSE) $(HOST_OLLAMA) up --build -d doom
	$(COMPOSE) $(HOST_OLLAMA) up --build harness

observe:       ## Like `up`, plus the Phoenix trace UI (http://localhost:6006) receiving every LLM call
	$(COMPOSE) --profile observability up --build -d doom ollama phoenix
	OTEL_EXPORTER_OTLP_ENDPOINT=http://phoenix:6006 $(COMPOSE) up --build harness

phoenix:       ## Start only the Phoenix trace UI (then add OBSERVE=1 to play/eval/compare)
	$(COMPOSE) --profile observability up -d phoenix
	@echo "Phoenix: http://localhost:$${PHOENIX_PORT:-6006}"

down:          ## Stop everything
	$(COMPOSE) --profile observability down

logs:          ## Follow the harness output
	$(COMPOSE) logs -f harness

play:          ## Run another batch of episodes (ARGS="--preset xs --scenario defend_the_center --episodes 5")
	$(COMPOSE) run --rm $(TRACE_ENV) harness play $(ARGS)

scripted:      ## Play with the rule-based baseline (no LLM needed)
	$(COMPOSE) run --rm harness play --policy scripted $(ARGS)

bench:         ## LLM vs scripted baseline over several scenarios
	$(COMPOSE) run --rm harness bench --compare $(ARGS)

eval:          ## Score model + playbook on fixed situations (ARGS="--preset s" or "--playbook my_playbook")
	$(COMPOSE) run --rm $(TRACE_ENV) harness eval $(ARGS)

compare:       ## Same games for the xs, s and m presets, then a comparison table (ARGS="--eval" for the quick check)
	$(COMPOSE) run --rm $(TRACE_ENV) harness compare $(ARGS)

report:        ## Outcome, LLM calls, tokens and latency of the latest run (ARGS="--last 3" or run dirs)
	$(COMPOSE) run --rm --no-deps harness report $(ARGS)

check:         ## Check the Doom server, Ollama and the model
	$(COMPOSE) run --rm harness check

prompt:        ## Print exactly what the model sees
	$(COMPOSE) run --rm harness prompt --new $(ARGS)

pull:          ## Download the models of all three presets (ARGS="--presets s" for one)
	$(COMPOSE) run --rm harness pull $(ARGS)

test:          ## Run the unit/integration tests locally
	cd server && python -m pytest -q
	cd harness && python -m pytest -q
