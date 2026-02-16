# Architecture Notes

- `src/webhook_server.py`: receives TradingView webhook JSON and validates HMAC + timestamp.
- `src/event_queue.py`: thread-safe queue bridge between Flask and bot.
- `src/bot_engine.py`: consumes queue events, builds universe, evaluates entries/exits, enforces risk and safety.
- `src/main.py`: process entrypoint; starts engine thread and Flask app.
