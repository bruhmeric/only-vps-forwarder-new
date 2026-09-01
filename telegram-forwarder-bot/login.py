"""Standalone Telethon login helper.

Run this ONCE before starting the bot:

    python login.py            # file-based session (default, for local dev)
    python login.py --string   # StringSession (recommended for Render)

File-based mode:
  * Saves the session as <SESSION_NAME>.session
  * Use this when running the bot locally with python bot.py

String mode (--string flag):
  * Uses Telethon's StringSession (no file written)
  * Prints the session string at the end
  * Copy that string into Render's SESSION_STRING environment variable
  * The same string works on any server — no file system required

In both modes, you'll be prompted for:
  * A confirmation code sent to your Telegram
  * (Optional) 2FA password if your account has it
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main() -> None:
    parser = argparse.ArgumentParser(description="Telethon login helper")
    parser.add_argument(
        "--string", action="store_true",
        help="Use StringSession and print it after login (recommended for Render)"
    )
    args = parser.parse_args()

    api_id_raw = os.environ.get("API_ID", "").strip()
    api_hash = os.environ.get("API_HASH", "").strip()
    phone = os.environ.get("PHONE", "").strip()
    session_name = os.environ.get("SESSION_NAME", "user_session").strip() or "user_session"

    if not api_id_raw or not api_hash:
        print("ERROR: API_ID and API_HASH must be set in .env")
        print("Get them from https://my.telegram.org/apps -> API development tools")
        sys.exit(1)

    try:
        api_id = int(api_id_raw)
    except ValueError:
        print(f"ERROR: API_ID must be an integer, got {api_id_raw!r}")
        sys.exit(1)

    if not phone:
        phone_input = input("Enter your phone number (with country code, e.g. +15551234567): ").strip()
        if not phone_input:
            print("ERROR: phone number is required")
            sys.exit(1)
        phone = phone_input

    if args.string:
        print("Connecting with StringSession...")
        client = TelegramClient(StringSession(), api_id, api_hash)
    else:
        print(f"Connecting with file-based session '{session_name}'...")
        client = TelegramClient(session_name, api_id, api_hash)

    await client.start(phone=phone)

    me = await client.get_me()
    print("\n=== Login successful ===")
    print(f"  User: @{getattr(me, 'username', None)} (id={me.id})")
    print(f"  Name: {getattr(me, 'first_name', '')} {getattr(me, 'last_name', '')}")

    if args.string:
        session_string = client.session.save()
        print()
        print("=" * 70)
        print("  SESSION_STRING (copy this ENTIRE line, including '1' prefix):")
        print()
        print(f"  {session_string}")
        print()
        print("=" * 70)
        print()
        print("  Next steps for Render:")
        print("    1. Copy the SESSION_STRING above")
        print("    2. In Render, go to your service -> Environment -> Add Environment Variable")
        print("    3. Key: SESSION_STRING  Value: (paste the string)")
        print("    4. Save and restart the service")
        print()
        print("  ⚠️  Treat this string like a password — it grants full")
        print("  access to your Telegram account.")
    else:
        print(f"\n  Session file: {session_name}.session")
        print("\n  You can now run `python bot.py` — it will reuse this session "
              "without prompting again.")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
