PYTHON ?= .venv/bin/python
TEXT ?= ""
QUERY ?= ""
PROMPT ?= ""
ARGS ?=
VSCODE_PROMPTS_DIR ?= $(HOME)/.vscode-server/data/User/prompts
MCP_HOST ?= 0.0.0.0
MCP_PORT ?= 8765
COMFYUI_DIR ?= $(HOME)/ComfyUI

.PHONY: doctor store retrieve chat session extract-store list list-signals list-task-runs model-report observe verify test test-chat-parity test-web-research test-task-readiness test-commit-pipeline test-context-evaluator test-response-evaluator test-post-turn-reflection assess-task mcp mcp-serve review-proposals recompute-weights recompute-all update delete dashboard comfyui reflect normalize-scope purge-stale users install-vscode-prompts reactivate push-convo

test:
	$(PYTHON) -m pytest tests/ -v

doctor:
	$(PYTHON) scripts/check_environment.py

store:
	$(PYTHON) scripts/store_memory.py $(TEXT) $(ARGS)

retrieve:
	$(PYTHON) scripts/retrieve_memory.py $(QUERY) $(ARGS)

chat:
	$(PYTHON) scripts/chat_with_memory.py $(PROMPT) $(ARGS)

session:
	$(PYTHON) scripts/chat_session.py $(ARGS)

extract-store:
	$(PYTHON) scripts/extract_and_store_memory.py $(TEXT) $(ARGS)

list:
	$(PYTHON) scripts/list_memories.py $(ARGS)

list-signals:
	$(PYTHON) scripts/list_signals.py $(ARGS)

list-task-runs:
	$(PYTHON) scripts/list_task_runs.py $(ARGS)

model-report:
	$(PYTHON) scripts/model_report.py $(ARGS)

observe:
	$(PYTHON) scripts/observe_model.py $(ARGS)

verify:
	$(PYTHON) scripts/verify_memory_layer.py $(ARGS)

test-chat-parity:
	$(PYTHON) scripts/test_chat_parity.py

test-web-research:
	$(PYTHON) scripts/test_web_research.py

test-task-readiness:
	$(PYTHON) scripts/test_task_readiness.py

test-commit-pipeline:
	$(PYTHON) scripts/test_commit_pipeline.py

test-context-evaluator:
	$(PYTHON) scripts/test_context_evaluator.py

test-response-evaluator:
	$(PYTHON) scripts/test_response_evaluator.py

test-post-turn-reflection:
	$(PYTHON) scripts/test_post_turn_reflection.py

assess-task:
	$(PYTHON) scripts/assess_task.py $(ARGS)

mcp:
	$(PYTHON) -m mcp_server.server

mcp-sse:
	MCP_TRANSPORT=sse MCP_PORT=$${MCP_PORT:-8765} $(PYTHON) -m mcp_server.server

mcp-serve:
	$(PYTHON) -m mcp_server.server --transport streamable-http --host $(MCP_HOST) --port $(MCP_PORT)

comfyui:
	$(COMFYUI_DIR)/.venv/bin/python $(COMFYUI_DIR)/main.py --listen 0.0.0.0 --port 8188

review-proposals:
	$(PYTHON) scripts/review_proposals.py $(ARGS)

recompute-weights:
	$(PYTHON) scripts/recompute_weights.py $(ARGS)

recompute-all:
	$(PYTHON) scripts/recompute_all_atoms.py

update:
	$(PYTHON) scripts/update_memory.py $(ARGS)

delete:
	$(PYTHON) scripts/delete_memory.py $(ARGS)

dashboard:
	FLASK_APP=dashboard.app .venv/bin/flask run --port 5001 --no-debugger

site:
	$(PYTHON) app_main.py

docker-up:
	docker compose up --build -d

docker-site:
	docker compose up --build -d db site

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f site

publish-trigger:
	$(PYTHON) -m app.publish_trigger $(ARGS)

cluster-discussions:
	$(PYTHON) -m app.discussion_clusterer $(ARGS)

reflect:
	$(PYTHON) scripts/reflect_task.py $(ARGS)

push-convo:
	$(PYTHON) scripts/push_conversation.py $(ARGS)

normalize-scope:
	$(PYTHON) scripts/normalize_scope.py $(ARGS)

purge-stale:
	$(PYTHON) scripts/purge_stale_atoms.py $(ARGS)

reactivate:
	$(PYTHON) scripts/reactivate_unresolved.py $(ARGS)

users:
	$(PYTHON) scripts/manage_users.py $(ARGS)

install-vscode-prompts:
	@mkdir -p "$(VSCODE_PROMPTS_DIR)"
	@cp prompts/memory-layer-workflow.instructions.md "$(VSCODE_PROMPTS_DIR)/"
	@echo "Installed → $(VSCODE_PROMPTS_DIR)/memory-layer-workflow.instructions.md"
