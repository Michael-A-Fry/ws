"""The Telegram flow, as a TOML string (a flow ships as Python data because `ws secret wall` copies only `.py` files)."""

TOML = """\
# Connect a Telegram bot: make it in BotFather, paste its token (stored, never shown), prove the token works, then bind
# one chat to it with a one-time link. Managed Bots (no paste) is the upgrade path: docs/decisions/0033.
schema = "ws.connect-flow/1"
id = "telegram"
title = "Connect a Telegram bot"
unique_key = "bot_id"

[secret]
name = "TELEGRAM_BOT_TOKEN"
hosts = ["api.telegram.org"]
purposes = ["notify"]
base_path = "/bot{value}"
paths = ["/getMe", "/getUpdates", "/sendMessage"]

[[steps]]
id = "make-bot"
type = "info"
title = "Make a bot"
text = "In Telegram, open BotFather, send /newbot, pick a name and a username that ends in bot. BotFather replies with a token: keep that message open."

[[steps]]
id = "open-botfather"
type = "link"
title = "Open BotFather"
text = "This opens the BotFather chat in Telegram."
url = "https://t.me/BotFather"

[[steps]]
id = "token"
type = "capture"
title = "Paste the bot token"
fields = [{ name = "token", label = "Bot token", secret = true, pattern = '[0-9]{5,}:[A-Za-z0-9_-]{30,}' }]
[steps.revoke]
action = "delete_secret"

[[steps]]
id = "who-am-i"
type = "verify"
title = "Check the token with Telegram"
identity = "result.username"
unique_id = "result.id"
[steps.request]
url = "https://api.telegram.org/bot{secret}/getMe"

[[steps]]
id = "bind-link"
type = "link"
title = "Start the bot from your chat"
text = "Open this link in Telegram and press Start. It binds that chat to your bot."
url = "https://t.me/{identity}?start={nonce}"
nonce = true
expires_s = 600

[[steps]]
id = "chat-bound"
type = "verify"
title = "Check the chat arrived"
[steps.request]
url = "https://api.telegram.org/bot{secret}/getUpdates"
[steps.conflict]
text = "Telegram says this bot has a webhook set, so ws can't read its messages. Removing the webhook stops whatever uses it now."
fix_url = "https://api.telegram.org/bot{secret}/deleteWebhook"
[steps.find]
list = "result"
where = { "message.text" = "/start {nonce}", "message.chat.type" = "private" }
take = { chat_id = "message.chat.id" }
[steps.revoke]
action = "tell"
text = "In Telegram, open BotFather and send /revoke to make the old token useless (ws deleted its copy). Then delete the bot with /deleteBot if you no longer want it."
"""
