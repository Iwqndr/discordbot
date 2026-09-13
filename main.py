import threading
from bot import bot
from dashboard import app, set_bot

set_bot(bot)


def run_flask():
    app.run(port=5000, debug=False, use_reloader=False)


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    print("- Web dashboard listening on http://localhost:5000")

    from bot import TOKEN
    bot.run(TOKEN)