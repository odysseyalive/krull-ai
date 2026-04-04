"""
Open WebUI Inlet Filter: Truth Guard
Injects honesty and intellectual integrity rules into every request.
Prevents the model from fabricating information, encourages it to ask
clarifying questions, and requires it to push back when the user is wrong.
"""

from pydantic import BaseModel, Field
from typing import Optional


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=-2,
            description="Filter priority (runs before all others)",
        )
        enabled: bool = Field(
            default=True, description="Enable/disable truth guard"
        )

    def __init__(self):
        self.valves = self.Valves()

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        if not self.valves.enabled:
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        truth_message = {
            "role": "system",
            "content": (
                "[Truth Guard — Intellectual Integrity Rules]\n\n"
                "1. DO NOT FABRICATE. If you don't know something, say so plainly. "
                "Never invent file paths, function names, URLs, facts, or code "
                "that you haven't verified. 'I don't know' is always an acceptable answer.\n\n"
                "2. ASK, DON'T GUESS. If the user's request is ambiguous or you lack "
                "the information to answer well, ask a clarifying question before proceeding. "
                "A good question is better than a wrong answer.\n\n"
                "3. FLAG UNCERTAINTY. When you're confident, say it directly. When you're "
                "uncertain, say so. 'I believe...' or 'I'm not sure, but...' is better "
                "than stating a guess as fact.\n\n"
                "4. PUSH BACK WHEN THE USER IS WRONG. If the user states something incorrect, "
                "makes a flawed assumption, or is heading toward a bad decision, say so directly "
                "and explain why. Being helpful means being honest, not agreeable.\n\n"
                "[End Truth Guard]"
            ),
        }

        messages.insert(0, truth_message)
        body["messages"] = messages

        return body
