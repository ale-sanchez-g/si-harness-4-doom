COMPOSE ?= docker compose
GPU := -f docker-compose.yml -f docker-compose.gpu.yml
HOST_OLLAMA := -f docker-compose.yml -f docker-compose.host-ollama.yml

.PHONY: up gpu host-ollama down logs play scripted bench eval check prompt pull test

up:            ## Start Doom + Ollama + harness (CPU) and follow the harness
	$(COMPOSE) up --build -d doom ollama
	$(COMPOSE) up --build harness

gpu:           ## Same, with an NVIDIA GPU for Ollama
	$(COMPOSE) $(GPU) up --build -d doom ollama
	$(COMPOSE) $(GPU) up --build harness

host-ollama:   ## Use the Ollama running on this machine (e.g. macOS app)
	$(COMPOSE) $(HOST_OLLAMA) up --build -d doom
	$(COMPOSE) $(HOST_OLLAMA) up --build harness

down:          ## Stop everything
	$(COMPOSE) down

logs:          ## Follow the harness output
	$(COMPOSE) logs -f harness

play:          ## Run another batch of episodes (ARGS="--scenario defend_the_center --episodes 5")
	$(COMPOSE) run --rm harness play $(ARGS)

scripted:      ## Play with the rule-based baseline (no LLM needed)
	$(COMPOSE) run --rm harness play --policy scripted $(ARGS)

bench:         ## LLM vs scripted baseline over several scenarios
	$(COMPOSE) run --rm harness bench --compare $(ARGS)

eval:          ## Score model + playbook on fixed situations (ARGS="--playbook my_playbook")
	$(COMPOSE) run --rm harness eval $(ARGS)

check:         ## Check the Doom server, Ollama and the model
	$(COMPOSE) run --rm harness check

prompt:        ## Print exactly what the model sees
	$(COMPOSE) run --rm harness prompt --new $(ARGS)

pull:          ## Download the model into the Ollama container
	$(COMPOSE) exec ollama ollama pull $${OLLAMA_MODEL:-granite4.2:3b}

test:          ## Run the unit/integration tests locally
	cd server && python -m pytest -q
	cd harness && python -m pytest -q
