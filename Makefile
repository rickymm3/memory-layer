PYTHON ?= .venv/bin/python
TEXT ?= ""
QUERY ?= ""
PROMPT ?= ""
ARGS ?=
VSCODE_PROMPTS_DIR ?= $(HOME)/.vscode-server/data/User/prompts

.PHONY: doctor store retrieve chat session extract-store list list-signals list-task-runs model-report test-chat-parity test-web-research test-task-readiness test-commit-pipeline test-context-evaluator test-response-evaluator test-post-turn-reflection assess-task mcp review-proposals recompute-weights recompute-all update delete dashboard reflect normalize-scope install-vscode-prompts

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

reflect:
	$(PYTHON) scripts/reflect_task.py $(ARGS)

normalize-scope:
	$(PYTHON) scripts/normalize_scope.py $(ARGS)

install-vscode-prompts:
	@mkdir -p "$(VSCODE_PROMPTS_DIR)"
	@cp prompts/memory-layer-workflow.instructions.md "$(VSCODE_PROMPTS_DIR)/"
	@echo "Installed → $(VSCODE_PROMPTS_DIR)/memory-layer-workflow.instructions.md"
