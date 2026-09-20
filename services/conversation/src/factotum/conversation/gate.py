"""Deciding whether a message refers to something said before (ADR 0012).

A small model reads the message alone, never the conversation, and says whether it can be understood
by itself, carries on the exchange just before, or points to something said earlier (with the words
to look for and, if the message names one, the day, week or month). Because it sees no history it is
cheap, and what an earlier exchange says cannot steer it.

It is asked in the ``chat.fast`` role, which falls back to ``chat.main`` where nothing faster was
bound. A failure is the caller's to weigh: a turn goes on without history rather than wait.
"""

import asyncio
from typing import Any

from pydantic import ValidationError

from factotum.contracts.chat import ChatMessage, ChatRequest, MessageRole
from factotum.contracts.conversation import Reference
from factotum.contracts.errors import GatewayError, ProviderTimeoutError
from factotum.contracts.identity import Owner
from factotum.contracts.models import ModelRole
from factotum.model_client import Chat

SYSTEM_PROMPT = """\
You look at one message that a person sent to their assistant and decide whether it depends on \
something said earlier in their conversation. You are not shown the conversation. Answer with JSON \
only.

"kind" is one of:
- "none": the message can be understood on its own, even when it is short or mentions the past in \
general. Most messages are like this.
- "continuation": it carries on the exchange that has just taken place and makes no sense without \
it: "and in English?", "why?", "the second one", "repeat that", "what about the other?".
- "earlier": it points to something said in a previous exchange, maybe a long time ago: "what you \
told me yesterday about the router", "the recipe you gave me last week", "that book you \
recommended".

Only for "earlier": "topic" is the words to look for in that exchange, in the language of the \
message, and "when" is the day, week or month the message names: "today", "yesterday", \
"this_week", "last_week", "this_month" or "last_month". Leave "topic" out when the message names \
nothing but a time, and "when" out when it names no time.

Examples:
"What is the capital of Australia?" -> {"kind": "none"}
"What did the Romans eat for breakfast?" -> {"kind": "none"}
"Turn on the kitchen lights" -> {"kind": "none"}
"Come si resetta il router?" -> {"kind": "none"}
"And in English?" -> {"kind": "continuation"}
"E il secondo?" -> {"kind": "continuation"}
"Why is that?" -> {"kind": "continuation"}
"What did you tell me yesterday about the router?" -> \
{"kind": "earlier", "topic": "router", "when": "yesterday"}
"La ricetta della carbonara che mi hai dato" -> {"kind": "earlier", "topic": "ricetta carbonara"}
"What did I ask you this morning?" -> {"kind": "earlier", "when": "today"}
"Come mi avevi consigliato di configurare il backup la settimana scorsa?" -> \
{"kind": "earlier", "topic": "configurare il backup", "when": "last_week"}
""".rstrip()

RESPONSE_SCHEMA: dict[str, Any] = Reference.model_json_schema()
"""What a model is asked to answer with: the shape of ``Reference``."""


class ReferenceGate:
    def __init__(self, chat: Chat, *, timeout_s: float = 8.0) -> None:
        self._chat = chat
        self._timeout_s = timeout_s

    async def decide(self, owner: Owner, message: str) -> Reference:
        """Whether ``message`` refers to an earlier exchange. Raises a ``FactotumError`` when it
        cannot be told: the gateway did not answer, the model did not answer in time or with
        something that is not a decision."""
        request = ChatRequest(
            role=ModelRole.CHAT_FAST,
            messages=(
                ChatMessage(role=MessageRole.SYSTEM, content=SYSTEM_PROMPT),
                ChatMessage(role=MessageRole.USER, content=message),
            ),
            response_schema=RESPONSE_SCHEMA,
            temperature=0,
            max_output_tokens=120,
        )
        try:
            async with asyncio.timeout(self._timeout_s):
                result = await self._chat.chat(owner, request)
        except TimeoutError:
            raise ProviderTimeoutError("the model did not decide in time") from None
        try:
            return Reference.model_validate(result.parsed)
        except ValidationError:
            raise GatewayError("the model's answer is not a decision about the message") from None
