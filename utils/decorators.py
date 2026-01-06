import functools
import logging
from typing import Callable, Any

from pyrogram.types import Message, CallbackQuery, User as PyrogramUser
from pyrogram import Client

import config
from db import get_user, add_user # get_user is essential here

LOGGER = logging.getLogger(__name__)

HandlerCallable = Callable[[Client, Any], Any]

def check_user_status(func: HandlerCallable) -> HandlerCallable:
    @functools.wraps(func)
    async def wrapper(client: Client, update: Message | CallbackQuery) -> Any:
        if not hasattr(update, "from_user") or not update.from_user:
            # For updates without a clear from_user (e.g. some channel posts if bot is admin, though rare for private bots)
            # Or if it's an inline query where from_user might be processed differently (see specific inline handler)
            LOGGER.warning(f"Update type {type(update)} does not have 'from_user' or it's None. Skipping user check.")
            return await func(client, update) # Proceed without user data if not applicable

        pyrogram_user: PyrogramUser = update.from_user
        user_id = pyrogram_user.id

        user_db_data = await get_user(user_id)

        if user_db_data and user_db_data.get("banned"):
            ban_reason = user_db_data.get("ban_reason", "No reason provided.")
            try:
                if isinstance(update, Message):
                    await update.reply_text(f"❌ You are banned.\nReason: {ban_reason}")
                elif isinstance(update, CallbackQuery):
                    await update.answer(f"❌ You are banned.\nReason: {ban_reason}", show_alert=True)
            except Exception as e:
                LOGGER.error(f"Error informing banned user {user_id} about ban: {e}")
            return None

        if not user_db_data:
            user_db_data = await add_user(
                user_id,
                first_name=pyrogram_user.first_name,
                username=pyrogram_user.username
            )
            if not user_db_data:
                err_msg = "⚠️ Account setup error. Please try /start again later."
                try:
                    if isinstance(update, Message): await update.reply_text(err_msg)
                    elif isinstance(update, CallbackQuery): await update.answer(err_msg, show_alert=True)
                except Exception as e:
                    LOGGER.error(f"Error sending account setup error to {user_id}: {e}")
                return None
            LOGGER.info(f"New user {user_id} ('{pyrogram_user.first_name}') added via decorator.")

        # Attach user_db_data for use in the handler
        # Note: Pyrogram's update objects are typically immutable or copied.
        # setattr might not always work as expected on the original object passed around.
        # A common pattern is to pass user_db_data as an additional argument to the handler,
        # or store it in a context (like `client.user_contexts[user_id]`) if a more global
        # approach is needed per request. For simplicity, we attempt setattr here.
        # If it doesn't persist, handlers should call get_user() themselves or we redesign.
        try:
            setattr(update, 'user_db', user_db_data) # Use 'user_db' to avoid conflict with update.from_user
        except AttributeError: # Happens if update object doesn't allow new attributes (e.g. frozen)
             # Fallback: Pass as kwarg if handler supports it, or log warning.
             # For now, we assume it works or handler will re-fetch. This is a common challenge.
             LOGGER.debug(f"Could not setattr 'user_db' on update object for user {user_id}. Handler might need to fetch manually.")


        return await func(client, update)
    return wrapper


def owner_only(func: HandlerCallable) -> HandlerCallable:
    @functools.wraps(func)
    async def wrapper(client: Client, update: Message | CallbackQuery) -> Any:
        if not hasattr(update, "from_user") or not update.from_user:
            return await func(client, update) # Allow if no user context to check against

        user_id = update.from_user.id
        if user_id != config.OWNER_ID:
            msg_text = "❌ Unauthorized: Owner access required."
            try:
                if isinstance(update, Message): await update.reply_text(msg_text)
                elif isinstance(update, CallbackQuery): await update.answer(msg_text, show_alert=True)
            except Exception as e:
                LOGGER.error(f"Error informing non-owner {user_id}: {e}")
            return None
        return await func(client, update)
    return wrapper

def sudo_users_only(func: HandlerCallable) -> HandlerCallable:
    """Restricts handler to Sudo users (defined in config, includes Owner)."""
    @functools.wraps(func)
    async def wrapper(client: Client, update: Message | CallbackQuery) -> Any:
        if not hasattr(update, "from_user") or not update.from_user:
            return await func(client, update) # Allow if no user context

        user_id = update.from_user.id

        # Sudo users list now directly from config includes owner
        if user_id not in config.SUDO_USERS:
            # Attempt to fetch user_db in case their role was set to sudo dynamically
            # and not present in config.SUDO_USERS (though config is source of truth for this decorator)
            user_db = getattr(update, 'user_db', await get_user(user_id)) # Fetch if not attached
            print(f"User DB: {user_db}")
            if not (user_db and user_db.get("is_sudo")): # Check DB 'is_sudo' as a fallback
                msg_text = "❌ Unauthorized: Sudo access required."
                try:
                    if isinstance(update, Message): await update.reply_text(msg_text)
                    elif isinstance(update, CallbackQuery): await update.answer(msg_text, show_alert=True)
                except Exception as e:
                    LOGGER.error(f"Error informing non-sudo {user_id}: {e}")
                return None
        return await func(client, update)
    return wrapper

def premium_users_only(func: HandlerCallable) -> HandlerCallable:
    @functools.wraps(func)
    async def wrapper(client: Client, update: Message | CallbackQuery) -> Any:
        if not hasattr(update, "from_user") or not update.from_user:
            return await func(client, update)

        user_id = update.from_user.id
        user_db = getattr(update, 'user_db', await get_user(user_id)) # Fetch if not attached

        if not user_db or not user_db.get("is_premium"):
            # If user_db is None (shouldn't happen if @check_user_status is used first), deny.
            # Or if user_db exists but "is_premium" is False or not set.
            msg_text = (
                "🌟 This feature is for Premium users. "
                f"Tap /start and check 'Go Premium' for more info!"
            ) # todo: add a button to main menu premium here
            try:
                if isinstance(update, Message): await update.reply_text(msg_text)
                elif isinstance(update, CallbackQuery):
                    await update.answer("🌟 Premium feature only.", show_alert=True)
                    if update.message: # Try sending a new message with more info
                         await update.message.reply_text(msg_text)
            except Exception as e:
                LOGGER.error(f"Error informing non-premium {user_id}: {e}")
            return None
        return await func(client, update)
    return wrapper