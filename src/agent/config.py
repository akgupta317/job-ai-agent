import os

from dotenv import load_dotenv

load_dotenv()

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
OLLAMA_TIMEOUT_S = float(os.getenv("OLLAMA_TIMEOUT_S", "300"))
OLLAMA_TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", "0.2"))
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))

# How many candidates go into one model call, and how many recommendations to write per side.
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "5"))
DEFAULT_MAX_PER_SIDE = int(os.getenv("DEFAULT_MAX_PER_SIDE", "5"))

# Existing titles sent to the model so it can flag duplicates. Each one costs reading time on
# CPU, so only the most related ones are sent. Set high (e.g. 500) to send them all.
MAX_EXISTING_TITLES = int(os.getenv("MAX_EXISTING_TITLES", "15"))

THRIVESTACK_MCP_URL = os.getenv("THRIVESTACK_MCP_URL", "https://api.app.thrivestack.ai/mcp")

# Local testing only: lets requests without an Authorization header fall back to a
# token from .env. Keep ALLOW_ENV_TOKEN=false everywhere else.
ALLOW_ENV_TOKEN = os.getenv("ALLOW_ENV_TOKEN", "false").lower() == "true"
DEV_MCP_TOKEN = os.getenv("THRIVESTACK_MCP_TOKEN", "")

# Judge agent: which model reviews quality, and how much work it may do per request.
OLLAMA_JUDGE_MODEL = os.getenv("OLLAMA_JUDGE_MODEL", OLLAMA_MODEL)
JUDGE_BATCH_SIZE = int(os.getenv("JUDGE_BATCH_SIZE", "3"))        # rows per model call (CPU is slow)
JUDGE_MAX_LLM_ROWS = int(os.getenv("JUDGE_MAX_LLM_ROWS", "12"))    # cap on rows sent to the model reviewer
JUDGE_SAVED_LIMIT = int(os.getenv("JUDGE_SAVED_LIMIT", "200"))     # saved recommendations reviewed per request

# Feedback loop: a pattern needs this many decided items before it influences anything.
FEEDBACK_MIN_DECIDED = int(os.getenv("FEEDBACK_MIN_DECIDED", "5"))
FEEDBACK_MAX_DISMISSED = int(os.getenv("FEEDBACK_MAX_DISMISSED", "6"))
FEEDBACK_MAX_ACCEPTED = int(os.getenv("FEEDBACK_MAX_ACCEPTED", "3"))

# Guardrails (agent/guard.py, llm/ollama.py). There is deliberately no "turn the guard off" setting.
MAX_QUESTION_CHARS = int(os.getenv("MAX_QUESTION_CHARS", "500"))
MAX_PROMPT_CHARS = int(os.getenv("MAX_PROMPT_CHARS", "40000"))   # hard cap on any single model call
MAX_ANSWER_CHARS = int(os.getenv("MAX_ANSWER_CHARS", "3000"))
LLM_WAIT_S = float(os.getenv("LLM_WAIT_S", "30"))                 # how long a request waits for the one model slot
