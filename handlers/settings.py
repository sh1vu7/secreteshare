import logging
from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message
from pyrogram.errors import MessageNotModified

import config
from db import get_user_setting, update_user_setting, get_user
from utils.keyboards import (
    create_settings_keyboard,
    SETTINGS_CALLBACK, # Entry point: "main:settings"
    SETTINGS_TOGGLE_PREFIX,
    MAIN_MENU_CALLBACK
)
from utils.decorators import check_user_status

LOGGER = logging.getLogger(__name__)

SETTINGS_TEXT_TEMPLATE = """
⚙️ **Your Settings - {bot_username}**

Manage your preferences for interacting with the bot.
Changes are saved instantly.

**Current Configuration:**
- _Notify on View_: {notify_on_view_status_text}
- _Default Content Protection (for new shares)_: {default_protected_content_status_text}
- _Default Forward Tag (for new shares)_: {default_show_forward_tag_status_text}

Click the buttons below to toggle settings.
"""

async def display_settings_menu(client: Client, cb_or_msg: CallbackQuery | Message, user_id: int):
    LOGGER.info(f"User {user_id} viewing settings.")
    user_db_data = await get_user(user_id) # get_user ensures settings obj exists and defaults merged

    if not user_db_data or "settings" not in user_db_data:
        LOGGER.error(f"Could not load settings for user {user_id} from DB.")
        err_msg = "Error: Could not load your settings."
        if isinstance(cb_or_msg, CallbackQuery): await cb_or_msg.answer(err_msg, show_alert=True)
        elif isinstance(cb_or_msg, Message): await cb_or_msg.reply_text(err_msg)
        return

    user_current_settings = user_db_data["settings"]

    text = SETTINGS_TEXT_TEMPLATE.format(
        bot_username=client.me.username,
        notify_on_view_status_text="🔔 Active" if user_current_settings.get("notify_on_view") else "🔕 Inactive",
        default_protected_content_status_text="🛡️ Yes (No Forward/Save)" if user_current_settings.get("default_protected_content") else "🔗 No (Allow)",
        default_show_forward_tag_status_text="🏷️ Show" if user_current_settings.get("default_show_forward_tag") else "Ẩ Hide"
    )
    keyboard = create_settings_keyboard(user_current_settings)

    try:
        if isinstance(cb_or_msg, CallbackQuery):
            await cb_or_msg.edit_message_text(text, reply_markup=keyboard)
            await cb_or_msg.answer()
        elif isinstance(cb_or_msg, Message):
            await cb_or_msg.reply_text(text, reply_markup=keyboard)
    except MessageNotModified:
        if isinstance(cb_or_msg, CallbackQuery): await cb_or_msg.answer("Settings are already up to date.")
    except Exception as e:
        LOGGER.error(f"Error displaying settings menu for {user_id}: {e}")
        if isinstance(cb_or_msg, CallbackQuery): await cb_or_msg.answer("Error loading settings.", show_alert=True)

@Client.on_callback_query(filters.regex(f"^{SETTINGS_CALLBACK}$")) # Catches "main:settings"
@check_user_status
async def settings_entry_handler(client: Client, cb: CallbackQuery):
    await display_settings_menu(client, cb, cb.from_user.id)

@Client.on_callback_query(filters.regex(f"^{SETTINGS_TOGGLE_PREFIX}"))
@check_user_status
async def settings_toggle_handler(client: Client, cb: CallbackQuery):
    user_id = cb.from_user.id
    try:
        # Regex will capture the setting_key, e.g. settings_toggle:notify_on_view -> notify_on_view
        setting_key = cb.data.split(SETTINGS_TOGGLE_PREFIX, 1)[1]
    except IndexError:
        LOGGER.error(f"Invalid setting toggle callback: {cb.data} for user {user_id}")
        await cb.answer("Error: Invalid setting action.", show_alert=True)
        return

    LOGGER.info(f"User {user_id} attempting to toggle setting: '{setting_key}'.")

    current_value = await get_user_setting(user_id, setting_key)

    if current_value is None or not isinstance(current_value, bool):
        # This check is vital. Ensure the setting_key from callback is actually in DEFAULT_USER_SETTINGS
        # and is a boolean type before toggling.
        if setting_key not in config.DEFAULT_USER_SETTINGS or \
           not isinstance(config.DEFAULT_USER_SETTINGS.get(setting_key), bool):
            await cb.answer(f"Error: '{setting_key}' is not a valid toggleable setting.", show_alert=True)
            LOGGER.warning(f"User {user_id} tried to toggle unknown or non-boolean setting: {setting_key}")
            return
        # If it's a known default boolean but somehow user's current_value is None (should not happen with get_user logic)
        current_value = config.DEFAULT_USER_SETTINGS[setting_key] # Fallback to default to allow toggle

    new_value = not current_value
    success = await update_user_setting(user_id, setting_key, new_value)

    if success:
        setting_display_name = setting_key.replace('_', ' ').title()
        await cb.answer(f"{setting_display_name}: {'Enabled' if new_value else 'Disabled'}")
        await display_settings_menu(client, cb, user_id) # Refresh menu
    else:
        await cb.answer(f"Error: Could not update '{setting_key}'.", show_alert=True)
        LOGGER.error(f"Failed to update_user_setting for {user_id}, key {setting_key} to {new_value}")