"""How many tokens a text will take, roughly.

The gateway does not count tokens and each model counts its own way. Chunks are sized against the
context a user declared for the embedding model, so the estimate leans high on purpose: text with
many symbols (code, commands, tables) takes more tokens per character than prose, and a chunk that
turns out shorter than it could have been costs a little precision, while one that is too long
costs an error.
"""

import math

CHARS_PER_TOKEN = 3.0


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))
