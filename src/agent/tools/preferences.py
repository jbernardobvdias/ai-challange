"""Lets the user's stated preferences carry over to later conversations."""
import json

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from agent.observability import emit
from agent.storage.profiles import Depth, ProfileStore, ResponseFormat


def make_preference_tools(profiles: ProfileStore) -> list[BaseTool]:
    """Tools for remembering and forgetting the user's preferences."""

    @tool
    def remember_preference(
        config: RunnableConfig,
        response_format: ResponseFormat | None = None,
        depth: Depth | None = None,
    ) -> str:
        """Remember how this user likes answers, for all future conversations. Use it when they state
        a lasting preference ("I prefer tables", "always keep it short"), not for a one-off request.

        Args:
            response_format: The layout they prefer.
            depth: How much detail they prefer.
        """
        user = config["configurable"]["user_id"]
        chosen = {"response_format": response_format, "depth": depth}
        saved = {name: value for name, value in chosen.items() if value is not None}
        if not saved:
            return json.dumps({"status": "error", "message": "No supported preference was given."})
        for name, value in saved.items():
            profiles.set(user, name, value)
        emit("preference_saved", **saved)
        return json.dumps({"status": "saved", **saved})

    @tool
    def forget_preferences(config: RunnableConfig) -> str:
        """Forget all of this user's saved preferences."""
        profiles.forget(config["configurable"]["user_id"])
        emit("preferences_forgotten")
        return json.dumps({"status": "forgotten"})

    return [remember_preference, forget_preferences]
