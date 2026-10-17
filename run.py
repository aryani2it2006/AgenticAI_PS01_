"""Start the Telegram bot + dashboard.   python run.py"""
import sys

from coordinator import config, db, server
from coordinator.bot import Coordinator
from coordinator.telegram_api import Telegram, TelegramError


def main():
    if not config.TELEGRAM_BOT_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN missing. Create a bot with @BotFather and put the token in .env (see README).")
    db.init()
    tg = Telegram(config.TELEGRAM_BOT_TOKEN)
    try:
        me = tg.get_me()
        tg.delete_webhook()
    except TelegramError as e:
        sys.exit(f"Could not reach Telegram: {e}")
    coord = Coordinator(tg, bot_username=me["username"])
    server.start(coord, config.HOST, config.PORT)
    coord.start_scheduler()
    mode = f"LLM + rules ({config.ANTHROPIC_MODEL})" if config.ANTHROPIC_API_KEY else "rules only (no ANTHROPIC_API_KEY)"
    print(f"Bot @{me['username']} is live | interpreter: {mode}")
    print(f"Dashboard: http://localhost:{config.PORT}   (team leaders register here)")
    try:
        tg.poll_forever(coord.handle_update)
    except KeyboardInterrupt:
        print("bye")


if __name__ == "__main__":
    main()
