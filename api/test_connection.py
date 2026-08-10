"""
A2T Test Connection API endpoint.
Token validation endpoint for the settings UI.
"""

import importlib

from helpers.api import ApiHandler, Request, Response
from usr.plugins.a2t.helpers.dependencies import ensure_dependencies


class YatcaTestConnection(ApiHandler):

    @classmethod
    def get_methods(cls) -> list[str]:
        return ["POST"]

    async def process(self, input: dict, request: Request) -> dict | Response:
        token = input.get("token", "") or (input.get("bot") or {}).get("token", "")
        if not token:
            return {"ok": False, "message": "Token is required"}

        import asyncio
        import sys
        # Force reimport of bot_manager to pick up code changes without framework restart
        for mod_name in list(sys.modules.keys()):
            if mod_name.startswith("usr.plugins.a2t.helpers.bot_manager"):
                del sys.modules[mod_name]
        importlib.invalidate_caches()

        await asyncio.to_thread(ensure_dependencies)
        from usr.plugins.a2t.helpers.bot_manager import test_token

        ok, message = await test_token(token)
        return {
            "ok": ok,
            "message": message,
            "results": [{"test": "Connection", "ok": ok, "message": message}],
        }
