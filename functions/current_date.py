"""
Open WebUI Inlet Filter: Current Date
Injects today's date into every request so the model
knows the current date and day of the week.
"""

from datetime import datetime
from pydantic import BaseModel, Field
from typing import Optional


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=-1, description="Filter priority (runs before all others)"
        )
        enabled: bool = Field(
            default=True, description="Enable/disable date injection"
        )

    def __init__(self):
        self.valves = self.Valves()

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        if not self.valves.enabled:
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        now = datetime.now()
        date_str = now.strftime("%A, %B %d, %Y")
        time_str = now.strftime("%I:%M %p")

        date_message = {
            "role": "system",
            "content": (
                f"Today's date is {date_str}. The current time is {time_str}. "
                f"This is a verified fact from the server clock, not a guess. "
                f"You MUST treat this as the actual current date when answering questions. "
                f"Do NOT say your data is outdated or that you cannot verify the date. "
                f"The date is {date_str}."
            ),
        }

        # Insert at the very beginning
        messages.insert(0, date_message)
        body["messages"] = messages

        return body
