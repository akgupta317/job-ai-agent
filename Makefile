run:
	PYTHONPATH=src uvicorn agent.main:app --reload --port 5301

test:
	PYTHONPATH=src pytest
