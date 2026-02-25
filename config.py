from typing import Literal


# Per-agent OpenAI-compatible settings
EXPLORER_OPENAI_MODEL_NAME = "MODEL"
TRAINER_OPENAI_MODEL_NAME = "MODEL"
EXPLORER_OPENAI_BASE_URL = "YOUR URL"  # Assuming the explorer is running on the local machine and has access to the GPU farm.
TRAINER_OPENAI_BASE_URL = "YOUR URL"
TRAINER_OPENAI_BASE_URL1 = "YOUR URL"

# Config dedicata per il summarize (memoria RAG) 
SUMMARIZE_OPENAI_BASE_URL = "YOUR URL"  # Sostituisci con l'endpoint corretto
SUMMARIZE_OPENAI_MODEL_NAME = "MODEL"  # oppure il modello che preferisci
# This configures what family of models we end up using.
MODEL: Literal["CLAUDE", "GEMINI", "OPENAI"] = "OPENAI"

# Currently only for the unused navigation_assistance feature. Can be ignored.
MAPPING_MODEL: Literal["CLAUDE", "GEMINI", "OPENAI"] = "OPENAI"


TEMPERATURE = 0.7
MAX_TOKENS = 400

# bypass using Claude for "navigate_to_offscreen_coordinate" because it's really token-expensive and also we know it can reliably do it if we give a HUGE amount of tokens,
# so it doesn't prove much. (plus I'd have to do streaming which is just annoying)
# This basically saves money at the cost of being a bit unsatisfying. This is a lot faster though.
DIRECT_NAVIGATION = True