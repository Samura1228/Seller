"""Generate USERBOT_SESSION for the selling account. Run on YOUR computer, not on Railway.

    pip install telethon
    python scripts/make_session.py

You will be asked for api_id / api_hash (my.telegram.org), the phone number of the SELLING account,
the login code Telegram sends to the app, and the 2FA password if set. Nothing is saved to disk:
the session string is only printed. Put it into Railway as USERBOT_SESSION and never share it.
"""

import asyncio
import getpass

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main() -> None:
    api_id = int(input("TG_API_ID: ").strip())
    api_hash = input("TG_API_HASH: ").strip()
    phone = input("Phone of the SELLING account (+357...): ").strip()

    client = TelegramClient(StringSession(), api_id, api_hash,
                            device_model="Limassol Seller", system_version="server", app_version="1.0")
    await client.start(
        phone=phone,
        code_callback=lambda: input("Code from Telegram: ").strip(),
        password=lambda: getpass.getpass("2FA password (empty if none): "),
    )
    me = await client.get_me()
    session = client.session.save()
    await client.disconnect()  # disconnect, NOT log_out: logging out would kill the session

    print(f"\nLogged in as @{me.username} (id {me.id}).")
    print("\nUSERBOT_SESSION (copy the whole line into Railway Variables):\n")
    print(session)
    print("\nDo not run any other script with this session while the bot is running on Railway.")


if __name__ == "__main__":
    asyncio.run(main())
