from __future__ import annotations

import json
import logging
import signal
import threading

from .bot_engine import FirstHourGapBotEngine
from .config import load_settings
from .event_queue import EventQueue
from .webhook_server import create_app


def configure_logging(level: str) -> logging.Logger:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("first_hour_gap_bot")
    logger.info(json.dumps({"msg": "logging_ready", "level": level}))
    return logger


def main() -> None:
    settings = load_settings()
    logger = configure_logging(settings.log_level)
    queue = EventQueue()

    engine = FirstHourGapBotEngine(settings=settings, event_queue=queue, logger=logger)
    thread = threading.Thread(target=engine.run_forever, daemon=True, name="bot-engine")
    thread.start()

    def _shutdown_handler(signum, frame):
        logger.info(json.dumps({"msg": "shutdown_signal", "signal": signum}))
        engine.shutdown()

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    app = create_app(settings=settings, event_queue=queue, logger=logger)
    app.run(host=settings.flask_host, port=settings.flask_port, threaded=True)


if __name__ == "__main__":
    main()
