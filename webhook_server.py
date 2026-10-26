"""Production webhook server implementation for MAX bot, replacing Long Polling with a persistent HTTPS webhook connection."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from aiohttp import web
import json

from config import settings
from core.router import Router
from max_api.client import MaxClient, MaxApiError
from max_api.models import Update
from handlers.passenger_new import register_passenger as register_passenger_handler
from handlers.driver import register_driver as register_driver_handler
from handlers.admin import register_admin as register_admin_handler

# Ensure UTF-8 encoding for output (Windows often cp1251)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
try:
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

logger = logging.getLogger("webhook")


async def health_check(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.json_response({"status": "ok", "service": "max-bot-webhook"})


async def _handle_webhook(request: web.Request) -> web.Response:
    """Webhook handler that converts aiohttp Request to max_api Update and dispatches."""
    try:
        data = await request.json()
        upd = Update.from_dict(data)
        # Get router from app context (set in create_app)
        router = request.app["router"]
        await router.dispatch(upd)
        return web.json_response({"status": "ok"})
    except Exception as e:
        logger.exception("Error handling webhook: %s", e)
        return web.json_response({"error": str(e)}, status=400)


def setup_routes(app: web.Application, router: Router) -> None:
    """Setup webhook routes and health check."""
    # Webhook endpoint for MAX API updates
    # Use a wrapper because router.dispatch expects Update object, not Request
    app.router.add_post("/se14566736_bot", _handle_webhook)

    # Health check
    app.router.add_get("/health", health_check)


def create_app(domain: str = "localhost:8080") -> web.Application:
    """Create and configure the web application."""
    app = web.Application()
    app["domain"] = domain
    app["route"] = "/se14566736_bot"

    # Initialize router
    client = MaxClient(token=settings.max_bot_token)
    router = Router(client)

    # Register handlers with the router
    register_passenger_handler(router)
    register_driver_handler(router)
    register_admin_handler(router)

    # Set router in app for access in setup_routes
    app["router"] = router

    # Setup routes
    setup_routes(app, router)

    return app


async def main() -> None:
    """Main entry point for webhook server."""
    # Validate configuration
    if not settings.max_bot_token:
        logger.error("MAX_BOT_TOKEN not configured! Set it in .env")
        sys.exit(1)

    if settings.environment == "development":
        domain = "localhost:8080"
    else:
        domain = os.getenv("PUBLIC_DOMAIN", "localhost:8080")

    # Create app
    app = create_app(domain)

    # Start server
    port = int(os.getenv("WEBHOOK_PORT", "8080"))
    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info("Webhook server started on 0.0.0.0:%s", port)
    logger.info("Webhook URL: https://%s%s", domain, app["route"])
    logger.info("Health check: http://your-domain/health")

    # Keep running
    try:
        await asyncio.Future()  # Run forever
    except KeyboardInterrupt:
        logger.info(" shutting down...")


if __name__ == "__main__":
    asyncio.run(main())