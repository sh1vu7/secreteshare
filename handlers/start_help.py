import logging
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, User as PyrogramUser
from pyrogram.errors import MessageNotModified

from config import OWNER_ID, DEFAULT_USER_SETTINGS, PREMIUM_TIER_MAX_FILE_SIZE_MB, FREE_TIER_MAX_FILE_SIZE_MB, PREMIUM_SELF_DESTRUCT_OPTIONS
from db import get_user, add_user # add_user for new users, get_user for fetching existing
from utils.keyboards import (
    create_main_menu_keyboard, create_help_keyboard,
    MAIN_MENU_CALLBACK, HELP_CALLBACK, MY_SECRETS_CALLBACK,
    SETTINGS_CALLBACK, SHARE_SECRET_CALLBACK, ADMIN_PANEL_CALLBACK, PREMIUM_CALLBACK
)
import config
from utils.decorators import check_user_status
from utils.user_states import clear_user_state

LOGGER = logging.getLogger(__name__)

START_MESSAGE_TEMPLATE = """
👋 Welcome, {user_mention}! I'm **{bot_username}**.

I help you share content privately. Your secrets are safe with me.

**Key Features:**
- Share messages, files, or media.
- Choose a specific recipient or generate a one-time link.
- Set self-destruct timers (Premium).
- Content becomes inaccessible after viewing or expiry.

Use the menu below to get started!
"""

HELP_MESSAGE = """
ℹ️ **{bot_username} Help Section**

**Getting Started:**
- `/start` - Shows the main menu.
- `/help` - Displays this help message.

**Main Menu Options:**
- **🔒 Share a Secret:** Begin the process of sharing content.
- **🗂️ My Shared Secrets:** View and manage secrets you've shared.
- **⚙️ Settings:** Customize your bot experience (e.g., view notifications, default share options).
- **🌟 Go Premium:** (If visible) Learn about premium benefits.
- **👑 Admin Panel:** (Sudo/Owner only) Access administrative functions.

**How Sharing Works:**
1.  Tap "🔒 Share a Secret".
2.  Choose content type (Message/File).
3.  Send the content.
4.  Select recipient (Specific User / Link).
5.  Set content protection (hide forward tag, prevent saving - *new!*).
6.  Choose a self-destruct timer (Premium users have more options).
7.  Confirm & Send!

Your privacy is paramount. Shared content is handled via Telegram's secure infrastructure.
"""

PREMIUM_INFO_MESSAGE_TEMPLATE = """
🌟 **Premium Benefits with {bot_username}** 🌟

Upgrade to unlock these great features:

- **📁 Larger Files:** Share up to **{premium_max_size}MB** (vs. {free_max_size}MB for free users).
- **⏱️ Advanced Timers:** More self-destruct options: {timer_options_str}.
- **🔗 Longer Link Lifespan:** Secrets shared via link can have extended expiry.
- **➕ Higher Limits:** Potentially more concurrent shares (currently {premium_concurrent} vs {free_concurrent}).
- _Future premium perks!_

Contact the bot owner ({owner_mention}) to inquire about Premium access.
"""

async def send_main_menu(client: Client, user_id: int, trigger_update: Message | CallbackQuery, edit: bool = False):
    user_db = await get_user(user_id) # Relies on get_user to handle defaults & status
    if not user_db: # Should ideally not happen if check_user_status is used
        LOGGER.error(f"User {user_id} not found in DB for send_main_menu. Attempting to add.")
        # Try to get pyrogram user object from trigger_update for first_name/username
        pyro_user_obj = trigger_update.from_user if hasattr(trigger_update, 'from_user') else None
        user_db = await add_user(
            user_id,
            first_name=pyro_user_obj.first_name if pyro_user_obj else "User",
            username=pyro_user_obj.username if pyro_user_obj else None
        )
        if not user_db:
            err_text = "Error accessing your profile. Please /start again."
            if isinstance(trigger_update, Message): await trigger_update.reply_text(err_text)
            elif isinstance(trigger_update, CallbackQuery): await trigger_update.answer(err_text, show_alert=True)
            return

    is_premium = user_db.get("is_premium", False)
    is_sudo = user_db.get("is_sudo", False) # Check DB sudo status

    keyboard = create_main_menu_keyboard(is_premium, is_sudo)
    start_text = START_MESSAGE_TEMPLATE.format(
        user_mention=trigger_update.from_user.mention if hasattr(trigger_update, 'from_user') and trigger_update.from_user else "User",
        bot_username=client.me.username
    )

    try:
        if isinstance(trigger_update, Message) and not edit: # Don't edit if it was a /start command, send new.
            await trigger_update.reply_text(start_text, reply_markup=keyboard, disable_web_page_preview=True)
        elif isinstance(trigger_update, CallbackQuery) or edit:
            # If trigger_update is a message but edit=True (e.g. from /cancelbroadcast)
            target_message = trigger_update.message if isinstance(trigger_update, CallbackQuery) else trigger_update
            if not target_message:
                 await client.send_message(user_id, start_text, reply_markup=keyboard, disable_web_page_preview=True)
                 if isinstance(trigger_update, CallbackQuery): await trigger_update.answer()
                 return

            try:
                await target_message.edit_text(start_text, reply_markup=keyboard, disable_web_page_preview=True)
            except MessageNotModified:
                if isinstance(trigger_update, CallbackQuery): await trigger_update.answer("Already on the main menu.")
            except Exception as e_edit: # If edit fails, send new.
                 LOGGER.warning(f"Failed to edit main menu for {user_id}, sending new: {e_edit}")
                 await client.send_message(user_id, start_text, reply_markup=keyboard, disable_web_page_preview=True)
                 # Optionally try to delete the old message with buttons if it was a callback
                 if isinstance(trigger_update, CallbackQuery) and trigger_update.message:
                      try: await trigger_update.message.delete()
                      except: pass
            if isinstance(trigger_update, CallbackQuery): await trigger_update.answer()
        else: # Fallback if logic above missed a case
             await client.send_message(user_id, start_text, reply_markup=keyboard, disable_web_page_preview=True)
             if isinstance(trigger_update, CallbackQuery): await trigger_update.answer()

    except Exception as e:
        LOGGER.error(f"Error sending/editing main menu for {user_id}: {e}")
        if isinstance(trigger_update, CallbackQuery):
            try: await trigger_update.answer("Error displaying menu.", show_alert=True)
            except: pass


@Client.on_message(filters.command("start") & filters.private)
@check_user_status # Ensures user is in DB and not banned; attaches user_db
async def start_command_handler(client: Client, message: Message):
    user_id = message.from_user.id
    LOGGER.info(f"User {user_id} ({message.from_user.first_name}) sent /start command.")
    clear_user_state(user_id) # Clear any pending multi-step operation

    if len(message.command) > 1:
        payload = message.command[1]
        if payload.startswith("viewsecret_"):
            # Defer to the share_flow handler for deep links
            # This is imported locally to avoid circular dependencies at module load time.
            from handlers.share_flow import process_view_secret_deep_link
            LOGGER.info(f"User {user_id} started with deep link payload: {payload}. Passing to deep link handler.")
            await process_view_secret_deep_link(client, message)
            return # The deep link handler will manage the response.
        elif payload.startswith("inline_"):
            # Future: Handle other deep links, e.g., for inline content generation or confirmation
            LOGGER.info(f"User {user_id} started with inline-related deep link: {payload}")
            # Placeholder: await process_inline_deep_link(client, message, payload)
            pass


    await send_main_menu(client, user_id, message, edit=False)


@Client.on_message(filters.command("help") & filters.private)
@check_user_status
async def help_command_handler(client: Client, message: Message):
    LOGGER.info(f"User {message.from_user.id} requested /help.")
    clear_user_state(message.from_user.id)
    keyboard = create_help_keyboard()
    help_text = HELP_MESSAGE.format(bot_username=client.me.username)
    await message.reply_text(help_text, reply_markup=keyboard, disable_web_page_preview=True)

@Client.on_callback_query(filters.regex(f"^{MAIN_MENU_CALLBACK}|^({HELP_CALLBACK})$"))
@check_user_status # Important for all callback handlers accessing user data
async def main_menu_navigation_handler(client: Client, cb: CallbackQuery):
    user_id = cb.from_user.id
    action_full = cb.data
    action_prefix = action_full.split(":")[0] + ":" if ":" in action_full else action_full

    LOGGER.debug(f"Main menu navigation: '{action_full}' by User {user_id}")

    if action_prefix == MAIN_MENU_CALLBACK:
        action = action_full.split(":")[1] if len(action_full.split(":")) > 1 else ""
        if action == "start":
            clear_user_state(user_id)
            await send_main_menu(client, user_id, cb, edit=True)
        elif action == "premium":
            owner_user = await client.get_users(OWNER_ID)
            owner_mention_str = owner_user.mention if owner_user else f"ID {OWNER_ID}"

            timer_options_str_list = []
            for minutes in PREMIUM_SELF_DESTRUCT_OPTIONS:
                if minutes == 0: label = "View-based/Max"
                elif minutes < 60: label = f"{minutes} min"
                elif minutes % 1440 == 0 : label = f"{minutes // 1440} day(s)"
                elif minutes % 60 == 0 : label = f"{minutes // 60} hour(s)"
                else: hours = minutes // 60; rem_minutes = minutes % 60; label = f"{hours}h{rem_minutes}m"
                timer_options_str_list.append(label)

            premium_text = PREMIUM_INFO_MESSAGE_TEMPLATE.format(
                bot_username=client.me.username,
                premium_max_size=PREMIUM_TIER_MAX_FILE_SIZE_MB,
                free_max_size=FREE_TIER_MAX_FILE_SIZE_MB,
                timer_options_str=", ".join(timer_options_str_list) if timer_options_str_list else "various options",
                owner_mention=owner_mention_str,
                premium_concurrent=config.MAX_CONCURRENT_SHARES_PREMIUM,
                free_concurrent=config.MAX_CONCURRENT_SHARES_FREE
            )
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data=f"{MAIN_MENU_CALLBACK}start")]])
            try:
                await cb.edit_message_text(premium_text, reply_markup=keyboard, disable_web_page_preview=True)
                await cb.answer()
            except Exception as e: # Fallback if edit fails
                LOGGER.warning(f"Failed to edit premium info, sending new: {e}")
                await cb.message.reply_text(premium_text, reply_markup=keyboard, disable_web_page_preview=True)
                if cb.message:
                    try:
                        await cb.message.delete()
                    except:
                        pass
                await cb.answer("Premium info loaded.")
        elif action == "share":
            LOGGER.info(f"Settings callback received from {user_id}. Deferring to share_handler.")
            from handlers.share_flow import initiate_share_handler
            await initiate_share_handler(client, cb)
            await cb.answer("Loading Share Options...") # Acknowledge and let dedicated handler take over
        elif action == "settings": # Matches "main:settings"
            LOGGER.info(f"Settings callback received from {user_id}. Deferring to settings_handler.")
            from handlers.settings import settings_entry_handler # Avoid direct import cycle
            await settings_entry_handler(client, cb)
            await cb.answer("Loading Settings...") # Acknowledge and let dedicated handler take over
        elif action == "help": # Matches "main:help"
            clear_user_state(user_id)
            keyboard = create_help_keyboard()
            help_text = HELP_MESSAGE.format(bot_username=client.me.username)
            try:
                await cb.edit_message_text(help_text, reply_markup=keyboard, disable_web_page_preview=True)
                await cb.answer()
            except Exception as e:
                LOGGER.warning(f"Failed to edit help message, sending new: {e}")
                await cb.message.reply_text(help_text, reply_markup=keyboard, disable_web_page_preview=True)
                if cb.message:
                    try:
                        await cb.message.delete()
                    except:
                        pass # Try to delete old button message
                await cb.answer("Help section loaded.")
        elif action == "my_secrets":
            LOGGER.info(f"My Secrets callback received from {user_id}. Deferring to my_secrets_handler.")
            from handlers.my_secrets import my_secrets_entry_handler
            await my_secrets_entry_handler(client, cb)
            await cb.answer("Loading Secret Lists...")
        else: # Unknown main menu action
             await cb.answer("Action not implemented yet.", show_alert=True)

    # elif action_prefix == HELP_CALLBACK: # Matches "main:help"
    #     clear_user_state(user_id)
    #     keyboard = create_help_keyboard()
    #     help_text = HELP_MESSAGE.format(bot_username=client.me.username)
    #     try:
    #         await cb.edit_message_text(help_text, reply_markup=keyboard, disable_web_page_preview=True)
    #         await cb.answer()
    #     except Exception as e:
    #         LOGGER.warning(f"Failed to edit help message, sending new: {e}")
    #         await cb.message.reply_text(help_text, reply_markup=keyboard, disable_web_page_preview=True)
    #         if cb.message:
    #             try:
    #                 await cb.message.delete()
    #             except:
    #                 pass # Try to delete old button message
    #         await cb.answer("Help section loaded.")
    else:
        # This branch should ideally not be reached if regexps are specific for MY_SECRETS, SETTINGS etc.
        # SHARE_SECRET_CALLBACK and ADMIN_PANEL_CALLBACK will be handled by their own dedicated handlers.
        # The regex for this handler is specifically `MAIN_MENU_CALLBACK` and `HELP_CALLBACK`.
        # Callbacks like "main:my_secrets" are distinct and handled by their respective modules.
        LOGGER.warning(f"Unexpected callback '{action_full}' reached main_menu_navigation_handler.")
        await cb.answer("Unknown action.", show_alert=True)


# Note: Callbacks like MY_SECRETS_CALLBACK ("main:my_secrets"), SETTINGS_CALLBACK ("main:settings"),
# SHARE_SECRET_CALLBACK ("main:share"), ADMIN_PANEL_CALLBACK ("admin:") are NOT handled here.
# They should have their own @Client.on_callback_query(filters.regex(f"^{THEIR_PREFIX}"))
# in their respective handler files (e.g., my_secrets.py, settings.py, share_flow.py, admin_panel.py).
# This keeps start_help.py focused on /start, /help, and basic main menu actions like showing premium info.
