import logging
import uuid
from datetime import datetime, timedelta, timezone
import re
from typing import Optional

from pyrogram import Client, filters, enums
from pyrogram.types import Message, CallbackQuery, User as PyrogramUser, InlineKeyboardMarkup, InlineKeyboardButton, InlineQuery, InlineQueryResultArticle, InputTextMessageContent
from pyrogram.errors import FloodWait, UserIsBlocked, PeerIdInvalid, MessageIdInvalid, ListenerTimeout, QueryIdInvalid, MessageNotModified

import config
from db import (
    get_user, shares_collection, create_share, update_share,
    get_user_setting, save_inline_share_content, get_inline_share_content,
    count_user_active_shares
)
from utils.keyboards import (
    create_share_type_keyboard, create_recipient_type_keyboard,
    create_protection_preferences_keyboard, create_self_destruct_options_keyboard,
    create_confirmation_keyboard, create_view_secret_button, create_max_views_keyboard,
    SHARE_SECRET_CALLBACK, MAIN_MENU_CALLBACK, SHARE_TYPE_PREFIX,
    RECIPIENT_TYPE_PREFIX, PROTECTION_PREF_PREFIX, FORWARD_TAG_TOGGLE_PREFIX,
    PROTECTED_CONTENT_TOGGLE_PREFIX, SET_DESTRUCT_PREFIX, SHARE_CONFIRM_PREFIX,
    SHARE_CANCEL_PREFIX, VIEW_SECRET_PREFIX, SET_MAX_VIEWS_PREFIX
)
from utils.decorators import check_user_status
from utils.user_states import (
    UserState, get_user_state, set_user_state, clear_user_state,
    start_share_flow, get_share_flow_data, update_share_flow_data,
    advance_share_flow_state
)
from utils.scheduler import schedule_message_deletion, schedule_share_expiry, cancel_scheduled_job, JOB_ID_PREFIX_EXPIRE_SHARE, JOB_ID_PREFIX_DELETE_MESSAGE
from handlers.start_help import send_main_menu # For cancellation navigation

LOGGER = logging.getLogger(__name__)
ASK_TIMEOUT_SECONDS = 300 # 5 minutes

async def cancel_current_share_flow(client: Client, user_id: int, trigger_update: CallbackQuery | Message, flow_data: Optional[dict]):
    share_uuid = flow_data.get("share_uuid") if flow_data else "unknown"
    current_state_name, _ = get_user_state(user_id)
    clear_user_state(user_id)
    text = "✅ Share process cancelled."
    LOGGER.info(f"Share flow (UUID: {share_uuid}, User: {user_id}, State: {current_state_name}) cancelled. State cleared.")

    target_message_for_edit = trigger_update.message if isinstance(trigger_update, CallbackQuery) else trigger_update
    try:
        if isinstance(trigger_update, CallbackQuery):
            await trigger_update.edit_message_text(text, reply_markup=None)
            await trigger_update.answer("Share cancelled.")
        else: # Message
            await trigger_update.reply_text(text)
    except Exception as e:
        LOGGER.warning(f"Error updating message on share cancel for {user_id}: {e}")
        await client.send_message(user_id, text) # Send as new message if edit fails

    # Send main menu after cancellation
    await send_main_menu(client, user_id, target_message_for_edit, edit=False) # Always send new menu after cancel text

# In handlers/share_flow.py

# Ensure these imports are at the top of your handlers/share_flow.py file
# from datetime import datetime, timezone (already there)
# from db import shares_collection, update_share, get_user_setting (already there, shares_collection might not be needed directly here if update_share used)
# from utils.scheduler import cancel_scheduled_job (new if not there)
# from pyrogram.errors import UserIsBlocked, PeerIdInvalid (already there)

@Client.on_message(filters.command("start") & filters.private & filters.create(lambda _, __, m: len(m.command) > 1 and m.command[1].startswith("viewsecret_")))
@check_user_status # Ensures user is in DB, not banned, and cb.user_db is available (though message.user_db used here)
async def process_view_secret_deep_link(client: Client, message: Message):
    viewer_id = message.from_user.id
    viewer_pyro_user = message.from_user # Pyrogram User object for name/mention

    try:
        access_token = message.command[1].split("viewsecret_", 1)[1]
    except IndexError:
        LOGGER.error(f"Invalid viewsecret deeplink payload: {message.text} for user {viewer_id}")
        await message.reply_text("⚠️ Invalid secret link format.")
        await send_main_menu(client, viewer_id, message) # Assuming send_main_menu is available
        return

    LOGGER.info(f"User {viewer_id} attempting to view secret via deeplink with token: {access_token}")

    # Fetch the share. Critical to check status and view count here.
    # Using find_one_and_update for atomicity if possible, but complex for pre-delivery checks with deep links.
    # Let's do a find, then an update if view is allowed.
    
    share = await shares_collection.find_one({"access_token": access_token})

    if not share:
        await message.reply_text("⚠️ This secret link is invalid or the secret no longer exists.")
        await send_main_menu(client, viewer_id, message)
        return

    # --- Initial validation of the share state ---
    if share["status"] != "active":
        status_msg = f"⚠️ This secret link has already been {share['status']} and is no longer available."
        await message.reply_text(status_msg)
        await send_main_menu(client, viewer_id, message)
        return

    # Specific recipient check for links that were *intended* for a specific user but shared via general link mechanism
    if share.get("recipient_id") and share.get("recipient_type") == "link" and share["recipient_id"] != viewer_id:
        # This scenario is less common for true deep links, more for links initially claimed then re-accessed
        await message.reply_text("🚫 This secret link seems to have been claimed by or intended for someone else.")
        await send_main_menu(client, viewer_id, message)
        return
    
    # Check max views limit
    current_view_count = share.get("view_count", 0)
    share_max_views = share.get("max_views", 1) # Default to 1 if not set

    if share_max_views > 0 and current_view_count >= share_max_views:
        LOGGER.info(f"Share {share['share_uuid']} (token {access_token}) via deeplink reached max_views ({current_view_count}/{share_max_views}). Not showing.")
        # Update status to reflect max views reached, then inform user
        await update_share(share["share_uuid"], {
            "status": "expired", # Or "max_views_reached" if you have such a status
            "expired_at": datetime.now(timezone.utc),
            "failure_reason": f"max_views_reached ({share_max_views})"
        })
        await message.reply_text("⚠️ This secret link has reached its maximum view limit and has been destroyed.")
        # Cancel link expiry job if it exists, as it's now definitively handled
        if share.get("expires_at"):
            job_id_link_expire = f"{JOB_ID_PREFIX_EXPIRE_SHARE}{share['share_uuid']}" # Using JOB_ID_PREFIX_EXPIRE_SHARE from scheduler
            cancel_scheduled_job(job_id_link_expire)
        await send_main_menu(client, viewer_id, message)
        return

    # --- Valid viewer, attempt to deliver secret ---
    # This is a critical section. We should update DB *before* delivering content ideally.
    viewer_name = viewer_pyro_user.first_name or f"User {viewer_id}"
    
    # Only update these fields if max_views > 0 and (current_view_count + 1) >= max_views
    update_fields = {}
    if share_max_views > 0 and (current_view_count + 1) >= share_max_views:
        update_fields = {
            "status": "viewed",  # Tentatively 'viewed', will become 'destructed' if this is the last view
            "viewed_at": datetime.now(timezone.utc),
            "viewed_by_user_id": viewer_id,
            "viewed_by_display_name": viewer_name,
            # view_count will be incremented with $inc
        }
    # If max_views == 0 (unlimited), do not update these fields
    
    # If it's a link being claimed by this view
    # Only update recipient_id/display_name for link shares if:
    # - max_views > 0 (not unlimited)
    # - and (current_view_count > share_max_views or (current_view_count + 1) == share_max_views)
    # This means: only on the last allowed view or if view count already exceeded (shouldn't normally happen)
    if (
        share.get("recipient_type") == "link"
        and not share.get("recipient_id")
        and share_max_views > 0
        and (
            current_view_count > share_max_views
            or (current_view_count + 1) == share_max_views
        )
    ):
        update_fields["recipient_id"] = viewer_id
        update_fields["recipient_display_name"] = viewer_name
        LOGGER.info(f"Link share {share['share_uuid']} (Token: {access_token}) being claimed by viewer {viewer_id} via deeplink.")

    # Atomically update view count and other fields
    # This ensures that even if multiple requests hit for the same link, view_count is accurate
    # Allow unlimited views if max_views is 0 or negative (premium unlimited), otherwise check view_count < max_views
    updated_share_doc = await shares_collection.find_one_and_update(
        {
            "access_token": access_token,
            "status": "active",
            "$or": [
                {"max_views": {"$lte": 0}},
                {"$expr": {"$lt": ["$view_count", "$max_views"]}}
            ]
        },
        {"$set": update_fields, "$inc": {"view_count": 1}},
        return_document=True  # Get the document *after* update
    )

    if not updated_share_doc:
        # This means the share was likely viewed by another request between the initial find and this update,
        # or its status changed, or view limit was hit by a concurrent request.
        LOGGER.warning(f"Share {share.get('share_uuid','N/A')} (token {access_token}) status changed or max_views hit before atomic update for deeplink user {viewer_id}.")
        await message.reply_text("⚠️ This secret was just accessed or expired. Please try again if you believe this is an error, or contact the sender.")
        await send_main_menu(client, viewer_id, message)
        return
        
    # Now updated_share_doc contains the share with incremented view_count
    share = updated_share_doc # Use the latest document
    
    await message.reply_text("🤫 Secret found! Revealing it momentarily...")
    
    try:
        source_chat_id = share["original_chat_id"]
        source_message_id = share["original_message_id"]
        
        send_kwargs = {"chat_id": viewer_id, "from_chat_id": source_chat_id, "message_id": source_message_id}
        
        # Use original_chat_id and original_message_id from the INLINE share doc (points to bot's "me" chat)
        # or from a normal share (points to sender's PM with bot)
        
        if not share.get("show_forward_tag", True): # Sender chose to hide forward tag
            if share.get("is_protected_content", False):
                 send_kwargs["protect_content"] = True
            await client.copy_message(**send_kwargs)
        else: # Show forward tag (default)
            # If is_protected_content=True here, behavior depends on Telegram. Bot cannot force protect on forward of unprotected message.
            await client.forward_messages(
                chat_id=send_kwargs["chat_id"],
                from_chat_id=send_kwargs["from_chat_id"],
                message_ids=[send_kwargs["message_id"]]
            )

        LOGGER.info(f"Secret {share['share_uuid']} content delivered to deeplink viewer {viewer_id}.")
        action_taken_message = "This secret has now been viewed."

        # Final status check: if this view met max_views, mark as destructed
        new_view_count = share.get("view_count", 0) # This count is now updated (from find_one_and_update)
        if share_max_views > 0 and new_view_count >= share_max_views:
            LOGGER.info(f"Share {share['share_uuid']} reached max_views ({new_view_count}/{share_max_views}) with this deeplink view.")
            await update_share(share["share_uuid"], {
                "status": "destructed", # Final status
                "destructed_at": datetime.now(timezone.utc),
                # Keep 'viewed_at', 'viewed_by_user_id' from the last view
            })
            action_taken_message = "This secret has reached its view limit and is now destroyed."
             # Cleanup the temp message from "me" chat if it's an inline share
            me = await client.get_me()
            if share.get("share_type") == "message_inline" and share.get("original_chat_id") == me.id:
                try: await client.delete_messages(share["original_chat_id"], share["original_message_id"])
                except Exception as e_del_tmp: LOGGER.warning(f"Could not delete inline temp msg {share['original_message_id']} after final view: {e_del_tmp}")


        # Notify sender if their setting allows
        sender_id = share.get("sender_id")
        if sender_id and await get_user_setting(sender_id, "notify_on_view"):
            try:
                await client.send_message(
                    sender_id,
                    f"ℹ️ Your secret (Link shared, ID: ...{share['share_uuid'][-6:]}) "
                    f"was just viewed by {viewer_name} (`{viewer_id}`)."
                )
            except Exception as e_notify:
                LOGGER.warning(f"Failed to send view notification for {share['share_uuid']}: {e_notify}")

        # Cancel the main link expiry job if it exists, as it's now viewed/destructed.
        if share.get("expires_at"): # Checks if share had a master expiry timer
            job_id_link_expire = f"{JOB_ID_PREFIX_EXPIRE_SHARE}{share['share_uuid']}"
            if cancel_scheduled_job(job_id_link_expire):
                LOGGER.info(f"Cancelled master expiry job for link share {share['share_uuid']} after view.")

        await message.reply_text(action_taken_message)

    except Exception as e:
        LOGGER.exception(f"Error revealing secret {access_token} (deeplink) to {viewer_id} after DB update: {e}")
        # DB already reflects the view attempt.
        await message.reply_text("📛 An error occurred while trying to show you the secret content after access was granted.")
    finally:
        # No main menu here as user interaction finished for this deep link
        pass


@Client.on_callback_query(filters.regex(f"^{SHARE_SECRET_CALLBACK}"))
@check_user_status
async def initiate_share_handler(client: Client, cb: CallbackQuery):
    user_id = cb.from_user.id
    user_db = cb.user_db # Attached by @check_user_status

    # Check concurrent shares limit
    # is_premium = user_db.get("is_premium", False)
    # limit = config.MAX_CONCURRENT_SHARES_PREMIUM if is_premium else config.MAX_CONCURRENT_SHARES_FREE
    # current_active = await count_user_active_shares(user_id)
    # if current_active >= limit:
    #     await cb.answer(f"You have reached the limit of {limit} active shares. Please wait or manage existing shares.", show_alert=True)
    #     return
    # Uncomment above if limits are to be strictly enforced from config

    LOGGER.info(f"User {user_id} initiated share secret flow.")
    share_uuid = start_share_flow(user_id) # State: AWAITING_SHARE_CONTENT
    update_share_flow_data(user_id,
                           sender_id=user_id,
                           # Initialize with user's default preferences
                           show_forward_tag=await get_user_setting(user_id, "default_show_forward_tag"),
                           is_protected_content=await get_user_setting(user_id, "default_protected_content")
                           )
    keyboard = create_share_type_keyboard(share_uuid)
    await cb.edit_message_text(
        "Let's share a secret! What kind of content is it?",
        reply_markup=keyboard
    )
    await cb.answer()

async def _handle_content_message_for_sharing(client: Client, user_id: int, content_message: Message, flow_data: dict):
    share_uuid = flow_data["share_uuid"]
    share_type = flow_data["share_type"] # Already set when share_type_selected was called

    user_db = await get_user(user_id) # Refresh user_db for premium check
    is_premium = user_db.get("is_premium", False)
    max_size_mb = config.PREMIUM_TIER_MAX_FILE_SIZE_MB if is_premium else config.FREE_TIER_MAX_FILE_SIZE_MB
    max_size_bytes = max_size_mb * 1024 * 1024
    original_file_name = None

    if share_type == "message":
        if not content_message.text or len(content_message.text) > config.MAX_MESSAGE_LENGTH_FOR_SECRET:
            await content_message.reply_text(
                f"⚠️ Text is empty or too long (max {config.MAX_MESSAGE_LENGTH_FOR_SECRET} chars). Share cancelled.")
            await cancel_current_share_flow(client, user_id, content_message, flow_data)
            return False
    elif share_type == "file":
        file_attr = (content_message.document or content_message.video or content_message.photo or
                     content_message.audio or content_message.voice or content_message.animation)
        if not file_attr:
            await content_message.reply_text("⚠️ No valid file/media found in the message. Share cancelled.")
            await cancel_current_share_flow(client, user_id, content_message, flow_data)
            return False

        file_size = getattr(file_attr, "file_size", 0)
        if file_size > max_size_bytes:
            await content_message.reply_text(
                f"⚠️ File too large ({file_size // (1024*1024)}MB). Your limit is {max_size_mb}MB. Share cancelled.")
            await cancel_current_share_flow(client, user_id, content_message, flow_data)
            return False
        original_file_name = getattr(file_attr, "file_name", None)
    else: # Should not happen
        await content_message.reply_text("Internal error: Invalid share type. Share cancelled.")
        await cancel_current_share_flow(client, user_id, content_message, flow_data)
        return False

    update_share_flow_data(user_id,
                           original_message_id=content_message.id,
                           original_chat_id=content_message.chat.id, # User's PM with bot
                           original_file_name=original_file_name)
    advance_share_flow_state(user_id, UserState.AWAITING_RECIPIENT)
    LOGGER.info(f"User {user_id} (Share UUID: {share_uuid}) provided content (Type: {share_type}). Asking for recipient.")

    keyboard = create_recipient_type_keyboard(share_uuid)
    await content_message.reply_text("👍 Content received! Who is this secret for?", reply_markup=keyboard)
    return True


@Client.on_callback_query(filters.regex(f"^{SHARE_TYPE_PREFIX}"))
@check_user_status
async def share_type_selected_handler(client: Client, cb: CallbackQuery):
    user_id = cb.from_user.id
    state, flow_data = get_user_state(user_id)
    
    try:
        share_type_choice = cb.data.split(":")[1]
        cb_share_uuid = cb.data.split(":")[2]
    except IndexError:
        await cb.answer("Invalid callback data.", show_alert=True); return

    if not flow_data or flow_data.get("share_uuid") != cb_share_uuid or state != UserState.AWAITING_SHARE_CONTENT:
        await cb.answer("Session mismatch or expired. Please /start again.", show_alert=True)
        await send_main_menu(client, user_id, cb, edit=True) # Send new menu
        return

    update_share_flow_data(user_id, share_type=share_type_choice)
    # State remains AWAITING_SHARE_CONTENT as we now use client.ask

    prompt_text = ""
    expected_filters = None
    if share_type_choice == "message":
        prompt_text = "✍️ Send the text message you want to share."
        expected_filters = filters.text
    elif share_type_choice == "file":
        prompt_text = "📎 Send the file, photo, video, or other media."
        expected_filters = filters.media # General media
    else:
        await cb.answer("Invalid share type selected.", show_alert=True); return

    cancel_btn_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data=f"{SHARE_CANCEL_PREFIX}now:{cb_share_uuid}")
    ]])
    await cb.edit_message_text(prompt_text + "\n\nOr you can cancel:", reply_markup=cancel_btn_markup)
    await cb.answer("Waiting for your content...")

    try:
        # This message will be a NEW message from the bot if cb.message is used for .ask prompt.
        # client.ask waits for a new message from user.
        content_msg: Message = await client.ask(
            chat_id=user_id,
            text="⏳ Please send your content now...", # This can be a new message.
            filters=expected_filters & filters.private & ~filters.command("cancel"), # Exclude /cancel command
            timeout=ASK_TIMEOUT_SECONDS
        )
        # Delete the "Please send your content now..." prompt from bot AFTER user replies.
        if content_msg.reply_to_message and content_msg.reply_to_message.from_user.is_self:
            try: await content_msg.reply_to_message.delete()
            except: pass # Might fail if user deleted it etc.
        
        # After getting content_msg, re-check state as it might have been cancelled during 'ask'
        current_state, current_flow_data = get_user_state(user_id)
        if current_state != UserState.AWAITING_SHARE_CONTENT or current_flow_data.get("share_uuid") != cb_share_uuid:
            LOGGER.info(f"Share {cb_share_uuid} cancelled or state changed during content ask for {user_id}.")
            # Cancellation already handled by cancel handler if button clicked
            return
        
        await _handle_content_message_for_sharing(client, user_id, content_msg, current_flow_data)

    except ListenerTimeout:
        state_after_timeout, data_after_timeout = get_user_state(user_id)
        if data_after_timeout.get("share_uuid") == cb_share_uuid and state_after_timeout == UserState.AWAITING_SHARE_CONTENT: # Still waiting for this share
            await client.send_message(user_id, f"⏰ Timeout. Share cancelled.")
            await cancel_current_share_flow(client, user_id, cb, data_after_timeout)
    except Exception as e:
        LOGGER.error(f"Error in content ask for {user_id}, share {cb_share_uuid}: {e}")
        state_after_error, data_after_error = get_user_state(user_id) # Fetch current state
        if data_after_error.get("share_uuid") == cb_share_uuid : # If error happened within this flow
             await client.send_message(user_id, "An error occurred. Share cancelled.")
             await cancel_current_share_flow(client, user_id, cb, data_after_error)

async def _handle_recipient_info(client: Client, user_id: int, recipient_info_message: Message, flow_data: dict):
    share_uuid = flow_data["share_uuid"]
    recipient_pyrogram_user: Optional[PyrogramUser] = None

    # if recipient_info_message.forward_from:
    #     recipient_pyrogram_user = recipient_info_message.from_user
    if recipient_info_message.text:
        txt = recipient_info_message.text.strip()
        try:
            if txt.startswith("@") or txt.isdigit():
                 # get_users can take username or ID (as int or string digit)
                recipient_pyrogram_user = await client.get_users(int(txt) if txt.isdigit() else txt)
            else: # Attempt to get by name, less reliable, might need disabling
                # users_found = await client.get_users(txt) # Could return a list
                # if users_found and isinstance(users_found, list) and users_found: recipient_pyrogram_user = users_found[0]
                # elif users_found and not isinstance(users_found, list): recipient_pyrogram_user = users_found
                await recipient_info_message.reply_text(
                    "⚠️ Invalid input. Please forward user's message, send @username, or User ID. Share cancelled."
                )
                await cancel_current_share_flow(client, user_id, recipient_info_message, flow_data); return False
        except PeerIdInvalid:
            await recipient_info_message.reply_text(f"⚠️ User '{txt}' not found or inaccessible. Share cancelled.")
            await cancel_current_share_flow(client, user_id, recipient_info_message, flow_data); return False
        except ValueError: # If int(txt) fails for non-digit string passed to int()
             await recipient_info_message.reply_text(f"⚠️ Invalid User ID format for '{txt}'. Share cancelled.")
             await cancel_current_share_flow(client, user_id, recipient_info_message, flow_data); return False
        except Exception as e:
            LOGGER.error(f"Error resolving recipient '{txt}' for {user_id}: {e}")
            await recipient_info_message.reply_text(f"⚠️ Could not process recipient. Share cancelled.")
            await cancel_current_share_flow(client, user_id, recipient_info_message, flow_data); return False
    else:
        await recipient_info_message.reply_text("⚠️ Please forward a message from the user, or send their @username/ID. Share cancelled.")
        await cancel_current_share_flow(client, user_id, recipient_info_message, flow_data); return False

    if not recipient_pyrogram_user:
        await recipient_info_message.reply_text("⚠️ Recipient not identified. Share cancelled.")
        await cancel_current_share_flow(client, user_id, recipient_info_message, flow_data); return False
    
    if recipient_pyrogram_user.is_self or recipient_pyrogram_user.is_bot:
        reason = "it's me!" if recipient_pyrogram_user.is_self else "it's a bot!"
        await recipient_info_message.reply_text(f"⚠️ Cannot share with this user ({reason}). Share cancelled.")
        await cancel_current_share_flow(client, user_id, recipient_info_message, flow_data); return False
    if recipient_pyrogram_user.id == user_id : # Double check
         await recipient_info_message.reply_text(f"⚠️ Cannot share a secret with yourself. Share cancelled.")
         await cancel_current_share_flow(client, user_id, recipient_info_message, flow_data); return False


    recipient_display_name = recipient_pyrogram_user.first_name or \
                             (f"@{recipient_pyrogram_user.username}" if recipient_pyrogram_user.username else f"User ID {recipient_pyrogram_user.id}")
    update_share_flow_data(user_id, recipient_id=recipient_pyrogram_user.id, recipient_display_name=recipient_display_name)
    
    # Move to Protection Preferences state
    advance_share_flow_state(user_id, UserState.AWAITING_PROTECTION_PREFERENCES)
    LOGGER.info(f"User {user_id} (Share: {share_uuid}) chose recipient {recipient_display_name}. Asking for protection prefs.")
    
    # Fetch current/default protection preferences
    current_show_tag = flow_data.get("show_forward_tag", await get_user_setting(user_id, "default_show_forward_tag"))
    current_protect_content = flow_data.get("is_protected_content", await get_user_setting(user_id, "default_protected_content"))

    keyboard = create_protection_preferences_keyboard(share_uuid, current_show_tag, current_protect_content)
    await recipient_info_message.reply_text(
        f"✅ Recipient: **{recipient_display_name}**.\n\n"
        "Next, delivery preferences (how the secret appears to them):",
        reply_markup=keyboard
    )
    return True

@Client.on_callback_query(filters.regex(f"^{RECIPIENT_TYPE_PREFIX}"))
@check_user_status
async def recipient_type_selected_handler(client: Client, cb: CallbackQuery):
    user_id = cb.from_user.id
    state, flow_data = get_user_state(user_id)

    try:
        recipient_type_choice = cb.data.split(":")[1]
        cb_share_uuid = cb.data.split(":")[2]
    except IndexError:
        await cb.answer("Invalid callback data.", show_alert=True); return

    if not flow_data or flow_data.get("share_uuid") != cb_share_uuid or state != UserState.AWAITING_RECIPIENT:
        await cb.answer("Session mismatch or expired. Please /start again.", show_alert=True)
        await send_main_menu(client, user_id, cb, edit=True)
        return

    update_share_flow_data(user_id, recipient_type=recipient_type_choice)
    # State remains AWAITING_RECIPIENT if client.ask is used for recipient info

    if recipient_type_choice == "link":
        # If link, skip recipient info, go to protection preferences directly
        advance_share_flow_state(user_id, UserState.AWAITING_PROTECTION_PREFERENCES)
        LOGGER.info(f"User {user_id} (Share: {cb_share_uuid}) chose 'link'. Asking for protection prefs.")
        current_show_tag = flow_data.get("show_forward_tag", await get_user_setting(user_id, "default_show_forward_tag")) # from initial flow start
        current_protect_content = flow_data.get("is_protected_content", await get_user_setting(user_id, "default_protected_content"))
        keyboard = create_protection_preferences_keyboard(cb_share_uuid, current_show_tag, current_protect_content)
        await cb.edit_message_text("🔗 Sharable link chosen.\n\nNext, delivery preferences:", reply_markup=keyboard)
        await cb.answer()
        return
    
    # If recipient_type_choice == "user":
    prompt_text = ("👤 To share with a specific user, forward one of their messages, "
                   "or send their @username or User ID.")
    cancel_btn_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data=f"{SHARE_CANCEL_PREFIX}now:{cb_share_uuid}")
    ]])
    await cb.edit_message_text(prompt_text + "\n\nOr cancel:", reply_markup=cancel_btn_markup)
    await cb.answer("Waiting for recipient's details...")

    try:
        recipient_info_msg: Message = await client.ask(
            chat_id=user_id,
            text="⏳ Please provide recipient's details...",
            filters=(filters.forwarded | filters.text) & filters.private & ~filters.command("cancel"),
            timeout=ASK_TIMEOUT_SECONDS
        )
        if recipient_info_msg.reply_to_message and recipient_info_msg.reply_to_message.from_user.is_self:
            try: await recipient_info_msg.reply_to_message.delete()
            except: pass

        current_state, current_flow_data = get_user_state(user_id)
        if current_state != UserState.AWAITING_RECIPIENT or current_flow_data.get("share_uuid") != cb_share_uuid:
            LOGGER.info(f"Share {cb_share_uuid} cancelled or state changed during recipient ask for {user_id}.")
            return

        await _handle_recipient_info(client, user_id, recipient_info_msg, current_flow_data)

    except ListenerTimeout:
        state_after_timeout, data_after_timeout = get_user_state(user_id)
        if data_after_timeout.get("share_uuid") == cb_share_uuid and state_after_timeout == UserState.AWAITING_RECIPIENT:
            await client.send_message(user_id, f"⏰ Timeout for recipient details. Share cancelled.")
            await cancel_current_share_flow(client, user_id, cb, data_after_timeout)
    except Exception as e:
        LOGGER.error(f"Error in recipient ask for {user_id}, share {cb_share_uuid}: {e}")
        state_after_error, data_after_error = get_user_state(user_id)
        if data_after_error.get("share_uuid") == cb_share_uuid :
             await client.send_message(user_id, "An error occurred getting recipient. Share cancelled.")
             await cancel_current_share_flow(client, user_id, cb, data_after_error)

@Client.on_callback_query(filters.regex(f"^{FORWARD_TAG_TOGGLE_PREFIX}|^{PROTECTED_CONTENT_TOGGLE_PREFIX}"))
@check_user_status
async def protection_toggle_handler(client: Client, cb: CallbackQuery):
    user_id = cb.from_user.id
    state, flow_data = get_user_state(user_id)

    try:
        # e.g. fwd_tag:share_uuid OR prot_cnt:share_uuid
        cb_share_uuid = cb.data.split(":", 1)[1]
        is_forward_tag_toggle = cb.data.startswith(FORWARD_TAG_TOGGLE_PREFIX)
        is_protected_content_toggle = cb.data.startswith(PROTECTED_CONTENT_TOGGLE_PREFIX)
    except IndexError:
        await cb.answer("Invalid callback data.", show_alert=True); return

    if not flow_data or flow_data.get("share_uuid") != cb_share_uuid or state != UserState.AWAITING_PROTECTION_PREFERENCES:
        await cb.answer("Session mismatch or expired. Please /start again.", show_alert=True)
        # Do not clear state here, could be another active flow. Cancel button specific to flow.
        await send_main_menu(client, user_id, cb, edit=True)
        return

    if is_forward_tag_toggle:
        new_val = not flow_data.get("show_forward_tag", await get_user_setting(user_id, "default_show_forward_tag"))
        update_share_flow_data(user_id, show_forward_tag=new_val)
        await cb.answer(f"Forward Tag: {'Show' if new_val else 'Hide'}")
    elif is_protected_content_toggle:
        new_val = not flow_data.get("is_protected_content", await get_user_setting(user_id, "default_protected_content"))
        update_share_flow_data(user_id, is_protected_content=new_val)
        await cb.answer(f"Protect Content: {'Yes' if new_val else 'No'}")

    # Refresh keyboard
    # Re-fetch flow_data as it was updated
    _, refreshed_flow_data = get_user_state(user_id) 
    keyboard = create_protection_preferences_keyboard(
        cb_share_uuid,
        refreshed_flow_data.get("show_forward_tag"),
        refreshed_flow_data.get("is_protected_content")
    )
    try:
        await cb.edit_message_reply_markup(reply_markup=keyboard)
    except MessageNotModified: pass
    except Exception as e:
        LOGGER.error(f"Error refreshing protection prefs keyboard for {user_id}: {e}")
        # May need to resend the whole message if only reply_markup edit fails significantly
        await cb.edit_message_text(cb.message.text.splitlines()[0], reply_markup=keyboard) # Try with existing text header


@Client.on_callback_query(filters.regex(f"^{PROTECTION_PREF_PREFIX}done:"))
@check_user_status
async def protection_prefs_done_handler(client: Client, cb: CallbackQuery):
    user_id = cb.from_user.id
    state, flow_data = get_user_state(user_id)
    try: cb_share_uuid = cb.data.split(":")[2]
    except IndexError: await cb.answer("Invalid callback.",show_alert=True); return

    if not flow_data or flow_data.get("share_uuid") != cb_share_uuid or state != UserState.AWAITING_PROTECTION_PREFERENCES:
        await cb.answer("Session error. Please /start over.", show_alert=True); return

    # Current preferences are already in flow_data from toggles or defaults
    advance_share_flow_state(user_id, UserState.AWAITING_SELF_DESTRUCT_CHOICE)
    LOGGER.info(f"User {user_id} (Share: {cb_share_uuid}) confirmed protection prefs. Asking for self-destruct.")

    user_db = cb.user_db # from @check_user_status
    is_premium = user_db.get("is_premium", False)
    keyboard = create_self_destruct_options_keyboard(cb_share_uuid, is_premium)
    await cb.edit_message_text(
        "⏰ How long should this secret exist before self-destructing?",
        reply_markup=keyboard
    )
    await cb.answer()

@Client.on_callback_query(filters.regex(f"^{SET_DESTRUCT_PREFIX}"))
@check_user_status
async def self_destruct_selected_handler(client: Client, cb: CallbackQuery):
    user_id = cb.from_user.id
    state, flow_data = get_user_state(user_id)

    # Regex allows for digits (minutes) or "0" (special meaning like view-based/max life for premium)
    match = re.match(rf"^{SET_DESTRUCT_PREFIX}([-\d]+):(.+)$", cb.data) 
    if not match:
        LOGGER.warning(f"Invalid callback data format for SET_DESTRUCT_PREFIX: {cb.data} by user {user_id}")
        await cb.answer("Invalid self-destruct selection format.", show_alert=True)
        return
    
    timer_choice_str, cb_share_uuid = match.groups()

    if not flow_data or flow_data.get("share_uuid") != cb_share_uuid or state != UserState.AWAITING_SELF_DESTRUCT_CHOICE:
        LOGGER.warning(f"Session/state mismatch for SET_DESTRUCT_PREFIX. User: {user_id}, CB_UUID: {cb_share_uuid}, State: {state.name}, FlowData UUID: {flow_data.get('share_uuid')}")
        await cb.answer("⚠️ Session error or invalid state. Please /start the share process over.", show_alert=True)
        # Optionally clear state if it seems stuck for this specific flow
        if flow_data.get("share_uuid") == cb_share_uuid:
            clear_user_state(user_id) # Clear this specific stuck flow
            await send_main_menu(client, user_id, cb, edit=True)
        return

    try:
        # self_destruct_minutes will store the actual minutes for scheduling,
        # or a convention like 0 for "view-based / max configured lifespan for premium".
        self_destruct_minutes = int(timer_choice_str)
    except ValueError:
        await cb.answer("Invalid timer value in selection.", show_alert=True)
        return

    user_db = cb.user_db # Attached by @check_user_status decorator
    is_premium = user_db.get("is_premium", False)
    
    # Determine the user-facing label and the actual minutes for internal use
    destruct_info_label = ""
    actual_destruct_minutes_for_scheduling = self_destruct_minutes # Start with user's choice

    if not is_premium:
        # # Free users get a default fixed expiry. Their choice is essentially just confirming this default.
        # # Or, if you offer limited choices, validate against those.
        # # Here, we assume free tier choice in keyboard maps to FREE_TIER_DEFAULT_EXPIRY_HOURS.
        # actual_destruct_minutes_for_scheduling = config.FREE_TIER_DEFAULT_EXPIRY_HOURS * 60
        # destruct_info_label = f"Default ({config.FREE_TIER_DEFAULT_EXPIRY_HOURS}h)"
        # # Ensure the `self_destruct_minutes` chosen by free user via button is aligned with config
        # if self_destruct_minutes != actual_destruct_minutes_for_scheduling :
        #      LOGGER.warning(f"Free user {user_id} selected {self_destruct_minutes} min, but forced to default {actual_destruct_minutes_for_scheduling} min.")
        #      self_destruct_minutes = actual_destruct_minutes_for_scheduling # Standardize what's stored in flow_data too
        if self_destruct_minutes == 0: 
            # For premium, "0 minutes" means view-based or max configured lifespan.
            # Store 0 or a sentinel value that indicates this.
            # The actual expiry for scheduling could be PREMIUM_TIER_MAX_EXPIRY_DAYS if no view occurs.
            actual_destruct_minutes_for_scheduling = config.FREE_TIER_MAX_EXPIRY_DAYS * 24 * 60 # Max possible time backup
            destruct_info_label = f"View-based (or max {config.FREE_TIER_MAX_EXPIRY_DAYS} days)"
        elif self_destruct_minutes in config.FREE_SELF_DESTRUCT_OPTIONS:
            # Valid premium choice from options
            if self_destruct_minutes < 60: destruct_info_label = f"{self_destruct_minutes} min"
            elif self_destruct_minutes % 1440 == 0: destruct_info_label = f"{self_destruct_minutes // 1440} day(s)"
            elif self_destruct_minutes % 60 == 0: destruct_info_label = f"{self_destruct_minutes // 60} hour(s)"
            else: destruct_info_label = f"{self_destruct_minutes // 60}h {self_destruct_minutes % 60}m"
            actual_destruct_minutes_for_scheduling = self_destruct_minutes
        else:
            # Invalid choice for premium (e.g., manipulated callback) - fallback to a default
            actual_destruct_minutes_for_scheduling = config.FREE_TIER_MAX_EXPIRY_DAYS * 24 * 60 # Max possible
            destruct_info_label = f"Default view-based (max {config.FREE_TIER_MAX_EXPIRY_DAYS} days)"
            LOGGER.warning(f"Free user {user_id} made invalid timer choice {self_destruct_minutes}. Defaulting.")
            self_destruct_minutes = 0 # Store 0 in flow_data to signify the 'view-based/max' choice for premium
    else: # Premium user
        if self_destruct_minutes == 0: 
            # For premium, "0 minutes" means view-based or max configured lifespan.
            # Store 0 or a sentinel value that indicates this.
            # The actual expiry for scheduling could be PREMIUM_TIER_MAX_EXPIRY_DAYS if no view occurs.
            actual_destruct_minutes_for_scheduling = config.PREMIUM_TIER_MAX_EXPIRY_DAYS * 24 * 60 # Max possible time backup
            destruct_info_label = f"View-based (or max {config.PREMIUM_TIER_MAX_EXPIRY_DAYS} days)"
        elif self_destruct_minutes in config.PREMIUM_SELF_DESTRUCT_OPTIONS:
            # Valid premium choice from options
            if self_destruct_minutes < 60: destruct_info_label = f"{self_destruct_minutes} min"
            elif self_destruct_minutes % 1440 == 0: destruct_info_label = f"{self_destruct_minutes // 1440} day(s)"
            elif self_destruct_minutes % 60 == 0: destruct_info_label = f"{self_destruct_minutes // 60} hour(s)"
            else: destruct_info_label = f"{self_destruct_minutes // 60}h {self_destruct_minutes % 60}m"
            actual_destruct_minutes_for_scheduling = self_destruct_minutes
        else:
            # Invalid choice for premium (e.g., manipulated callback) - fallback to a default
            actual_destruct_minutes_for_scheduling = config.PREMIUM_TIER_MAX_EXPIRY_DAYS * 24 * 60 # Max possible
            destruct_info_label = f"Default view-based (max {config.PREMIUM_TIER_MAX_EXPIRY_DAYS} days)"
            LOGGER.warning(f"Premium user {user_id} made invalid timer choice {self_destruct_minutes}. Defaulting.")
            self_destruct_minutes = 0 # Store 0 in flow_data to signify the 'view-based/max' choice for premium

    update_share_flow_data(user_id,
                           self_destruct_minutes_set=self_destruct_minutes, # The user's choice or derived standard value (e.g. 0 for premium special)
                           self_destruct_minutes_for_scheduling=actual_destruct_minutes_for_scheduling, # Actual minutes for scheduler if timer based
                           self_destruct_label=destruct_info_label)
    
    # Advance to AWAITING_MAX_VIEWS_CHOICE
    advance_share_flow_state(user_id, UserState.AWAITING_MAX_VIEWS_CHOICE)
    LOGGER.info(f"User {user_id} (Share: {cb_share_uuid}) set self-destruct: {destruct_info_label}. Actual minutes if timed: {actual_destruct_minutes_for_scheduling}. Asking for max views.")
    
    keyboard_max_views = create_max_views_keyboard(cb_share_uuid, is_premium)
    prompt_text_max_views = (
        f"⏰ Timer set to: **{destruct_info_label}**.\n\n"
        "👁️ Now, how many times can this secret be viewed before it's destroyed?\n"
        "(This is independent of the time limit.)"
    )
    try:
        await cb.edit_message_text(prompt_text_max_views, reply_markup=keyboard_max_views)
        await cb.answer()
    except Exception as e:
        LOGGER.error(f"Error editing message for max views choice (user {user_id}, share {cb_share_uuid}): {e}")
        await cb.answer("Error proceeding to max views. Please try share again.", show_alert=True)
        clear_user_state(user_id) # Clear state on error here to avoid being stuck
        await send_main_menu(client, user_id, cb, edit=True) # Go back to main menu

# Add this new handler function
@Client.on_callback_query(filters.regex(f"^{SET_MAX_VIEWS_PREFIX}"))
@check_user_status
async def max_views_selected_handler(client: Client, cb: CallbackQuery):
    user_id = cb.from_user.id
    state, flow_data = get_user_state(user_id)

    match = re.match(rf"^{SET_MAX_VIEWS_PREFIX}(\d+):(.+)$", cb.data) # \d+ for positive integers (0 for unlimited)
    if not match:
        await cb.answer("Invalid max views selection.", show_alert=True); return
    
    views_choice_str, cb_share_uuid = match.groups()

    if not flow_data or flow_data.get("share_uuid") != cb_share_uuid or state != UserState.AWAITING_MAX_VIEWS_CHOICE:
        await cb.answer("Session error. Please /start over.", show_alert=True); return

    try:
        max_views = int(views_choice_str)
        if max_views < 0: raise ValueError("Max views cannot be negative") # 0 is allowed for unlimited (premium)
    except ValueError:
        await cb.answer("Invalid views value.", show_alert=True); return

    user_db = cb.user_db
    is_premium = user_db.get("is_premium", False)

    # Validate choice against tier limits
    if not is_premium:
        if max_views == 0 or max_views > config.FREE_TIER_MAX_ALLOWED_MAX_VIEWS:
            max_views = config.FREE_TIER_DEFAULT_MAX_VIEWS # Default for free if invalid choice
            await cb.answer(f"Set to default {max_views} views for your tier.", show_alert=True)
    else: # Premium user
        if max_views not in config.PREMIUM_MAX_VIEWS_OPTIONS and max_views != 0: # If specific options defined
             # Fallback if they somehow sent a value not in options.
             max_views = config.PREMIUM_TIER_DEFAULT_MAX_VIEWS
             await cb.answer(f"Invalid option. Set to default {max_views} views.", show_alert=True)
        elif max_views == 0: # 0 for unlimited (premium only)
            pass # Will be handled in DB as e.g. -1 or a very large number, or no max_views field

    views_label = f"{max_views} View{'s' if max_views != 1 else ''}"
    if is_premium and max_views == 0:
        views_label = "Unlimited Views"
        # Internally, you might store 0, or a very high number, or None for max_views
        # to signify unlimited. For DB schema, maybe max_views = 0 or -1 means unlimited.
        # Let's say `max_views = 0` stored in DB means unlimited for this example.

    update_share_flow_data(user_id, max_views=max_views, max_views_label=views_label)
    advance_share_flow_state(user_id, UserState.AWAITING_CONFIRMATION)
    LOGGER.info(f"User {user_id} (Share: {cb_share_uuid}) chose max views: {views_label} ({max_views}).")

    # Now, build and show the confirmation text (this is from self_destruct_selected_handler, now moved here)
    # Fetch fresh flow_data because it was just updated
    _, current_flow_data = get_user_state(user_id) 

    confirm_text = "🔒 **Confirm Your Secret Share**\n\n"
    content_desc = "Text Message"
    if current_flow_data['share_type'] == 'file':
        content_desc = f"File/Media ({current_flow_data.get('original_file_name', 'N/A')})"
    confirm_text += f"▫️ **Content:** {content_desc}\n"

    if current_flow_data['recipient_type'] == "user":
        confirm_text += f"▫️ **Recipient:** {current_flow_data.get('recipient_display_name', 'N/A')}\n"
    else: # link
        confirm_text += "▫️ **Recipient:** Sharable Link\n"
    
    confirm_text += f"▫️ **Forward Tag:** {'Shown' if current_flow_data.get('show_forward_tag') else 'Hidden'}\n"
    confirm_text += f"▫️ **Content Protection:** {'Enabled' if current_flow_data.get('is_protected_content') else 'Disabled'}\n"
    confirm_text += f"▫️ **Self-Destruct Timer:** {current_flow_data.get('self_destruct_label', 'Default/View-Based')}\n"
    confirm_text += f"▫️ **Max Views:** {views_label}\n\n" # Added Max Views
    confirm_text += "Please confirm to send."

    keyboard = create_confirmation_keyboard(cb_share_uuid)
    await cb.edit_message_text(confirm_text, reply_markup=keyboard)
    await cb.answer()

@Client.on_callback_query(filters.regex(f"^{SHARE_CONFIRM_PREFIX}send:") | filters.regex(f"^{SHARE_CANCEL_PREFIX}now:"))
@check_user_status
async def confirmation_final_handler(client: Client, cb: CallbackQuery):
    user_id = cb.from_user.id
    state, flow_data = get_user_state(user_id)
    is_cancel = cb.data.startswith(SHARE_CANCEL_PREFIX)

    try:
        # confirm:send:uuid OR cancel:now:uuid
        cb_share_uuid = cb.data.split(":", 2)[2]
    except IndexError: await cb.answer("Invalid action data.", show_alert=True); return

    if not flow_data or flow_data.get("share_uuid") != cb_share_uuid:
        await cb.answer("Session error. Please /start over.", show_alert=True); return

    if is_cancel:
        await cancel_current_share_flow(client, user_id, cb, flow_data)
        return

    # --- Process CONFIRM ---
    if state != UserState.AWAITING_CONFIRMATION:
        await cb.answer("Invalid confirmation state. Please /start over.", show_alert=True); return
        
    LOGGER.info(f"User {user_id} CONFIRMED share {cb_share_uuid}. Finalizing.")
    await cb.edit_message_text("Processing your secret... Please wait.", reply_markup=None) # Temp message
    
    # Ensure all necessary data is present
    required_keys = ['share_uuid', 'sender_id', 'share_type', 'original_message_id', 'original_chat_id',
                     'recipient_type', 'show_forward_tag', 'is_protected_content', 'self_destruct_minutes_set']
    if not all(key in flow_data for key in required_keys):
        LOGGER.error(f"Missing critical data in flow_data for {cb_share_uuid}: {flow_data}")
        await cb.message.reply_text("Critical error: Share data incomplete. Please try again.")
        clear_user_state(user_id); return


    access_token = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    expires_at_datetime = None
    if flow_data["self_destruct_minutes_set"] > 0 :
        expires_at_datetime = now + timedelta(minutes=flow_data["self_destruct_minutes_set"])

    db_share_doc = {
        "share_uuid": flow_data["share_uuid"], "access_token": access_token,
        "sender_id": user_id, "sender_mention": cb.from_user.first_name or f"User {user_id}",
        "recipient_id": flow_data.get("recipient_id"),
        "recipient_display_name": flow_data.get("recipient_display_name"),
        "recipient_type": flow_data["recipient_type"],
        "share_type": flow_data["share_type"],
        "original_message_id": flow_data["original_message_id"],
        "original_chat_id": flow_data["original_chat_id"],
        "original_file_name": flow_data.get("original_file_name"),
        "show_forward_tag": flow_data["show_forward_tag"],
        "is_protected_content": flow_data["is_protected_content"],
        "bot_message_id_to_recipient": None, # Will be filled if applicable
        "status": "active", "created_at": now, "expires_at": expires_at_datetime,
        "self_destruct_after_view": True, # Standard policy for this bot
        "self_destruct_minutes_set": flow_data["self_destruct_minutes_set"],
        "view_count": 0,
        "max_views": flow_data.get("max_views", 1), # Default to 1 if not set
    }

    sent_to_recipient_msg_id = None
    final_share_message_text = ""

    try:
        if db_share_doc["recipient_type"] == "link":
            bot_username = client.me.username
            sharable_link_url = f"https://t.me/{bot_username}?start=viewsecret_{access_token}"
            final_share_message_text = (
                f"✅ Secret link ready!\n\nThis one-time link will reveal your secret:\n"
                f"{sharable_link_url}\n\nIt self-destructs based on timer or after view."
            )
            LOGGER.info(f"Share {cb_share_uuid} created as link: {sharable_link_url}")
        
        elif db_share_doc["recipient_id"]: # Specific user
            recipient_chat_id_int = db_share_doc["recipient_id"]
            view_button_text = f"🤫 {db_share_doc['sender_mention']} shared a secret!"
            view_secret_kb = create_view_secret_button(access_token, custom_text=view_button_text)
            
            # This control message will itself be scheduled for deletion if share has timer
            sent_control_msg = await client.send_message(
                chat_id=recipient_chat_id_int,
                text=(f"🔒 You have a new secret from {db_share_doc['sender_mention']}.\n"
                      "Click below. It may self-destruct after view or time."),
                reply_markup=view_secret_kb,
                protect_content=True # The "View Secret" button message itself is protected
            )
            sent_to_recipient_msg_id = sent_control_msg.id
            db_share_doc["bot_message_id_to_recipient"] = sent_to_recipient_msg_id
            final_share_message_text = (
                f"✅ Secret sent to {db_share_doc['recipient_display_name']}!\n"
                f"They'll receive a button to view it."
            )
            LOGGER.info(f"Control message for share {cb_share_uuid} sent to {recipient_chat_id_int}, msg_id: {sent_to_recipient_msg_id}")
            
            # Schedule deletion of this control message if there's an expiry timer on the share
            if expires_at_datetime:
                 await schedule_message_deletion(
                     client, recipient_chat_id_int, sent_to_recipient_msg_id,
                     expires_at_datetime, db_share_doc["share_uuid"]
                 )
        else:
            raise ValueError("Invalid recipient configuration for share.")

        created_share_doc = await create_share(db_share_doc)
        if not created_share_doc:
            raise Exception("Failed to save share to DB.")
        
        # Schedule master expiry for link-based shares (if timer set)
        if db_share_doc["recipient_type"] == "link" and expires_at_datetime:
             await schedule_share_expiry(client, db_share_doc["share_uuid"], expires_at_datetime)
            
        await cb.message.edit_text(
            final_share_message_text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Main Menu", callback_data=f"{MAIN_MENU_CALLBACK}start")]])
        )
        await cb.answer("Secret processed!")

    except (UserIsBlocked, PeerIdInvalid) as e_user:
        err_user_msg = f"Could not send to {flow_data.get('recipient_display_name', 'user')}: "
        err_user_msg += "they blocked the bot or an invalid ID was provided."
        LOGGER.warning(f"Share {cb_share_uuid} failed: {e_user}")
        await cb.message.edit_text(f"⚠️ {err_user_msg} Share cancelled.", reply_markup=None)
        # Note: DB record for share is NOT created in this case
    except Exception as e:
        LOGGER.exception(f"Critical error finalizing share {cb_share_uuid} for {user_id}: {e}")
        await cb.message.edit_text("⚠️ Critical error processing secret. Please try later.", reply_markup=None)
        # Consider deleting partial DB entry if one was made before error
    finally:
        clear_user_state(user_id)


@Client.on_callback_query(filters.regex(f"^{VIEW_SECRET_PREFIX}"))
@check_user_status # User clicking button
async def view_secret_button_handler(client: Client, cb: CallbackQuery):
    viewer_id = cb.from_user.id
    viewer_pyro_user = cb.from_user
    access_token = cb.data[len(VIEW_SECRET_PREFIX):] # Extract token from callback_data

    LOGGER.info(f"User {viewer_id} clicked 'View Secret' button, token: {access_token}")

    # Use find_one_and_update to atomically check status, view limits, and update.
    # This is the most crucial part for preventing race conditions on button clicks.
    
    # Fields to set on successful view registration (some might be conditional)
    viewer_name = viewer_pyro_user.first_name or f"User {viewer_id}"
    set_on_view_fields = {}

    # Fetch the share to check its recipient_type for potential recipient_id update
    # This initial fetch is okay before the atomic update.
    initial_share_check = await shares_collection.find_one({"access_token": access_token})

    if not initial_share_check:
        await cb.answer("⚠️ This secret does not exist or was already destroyed.", show_alert=True)
        try: await cb.message.delete() # Delete the button message as it's invalid
        except: pass
        return

    share_max_views = initial_share_check.get("max_views", 1)
    current_view_count = initial_share_check.get("view_count", 0)
    if share_max_views > 0 and (current_view_count + 1) >= share_max_views:
        set_on_view_fields = {
            "status": "viewed",  # Tentatively 'viewed', will become 'destructed' if this is the last view
            "viewed_at": datetime.now(timezone.utc),
            "viewed_by_user_id": viewer_id,
            "viewed_by_display_name": viewer_name,
            # view_count will be incremented with $inc
        }
    
    # Fetch the share to check its recipient_type for potential recipient_id update
    # This initial fetch is okay before the atomic update.
    initial_share_check = await shares_collection.find_one({"access_token": access_token})

    if not initial_share_check:
        await cb.answer("⚠️ This secret does not exist or was already destroyed.", show_alert=True)
        try: await cb.message.delete() # Delete the button message as it's invalid
        except: pass
        return
        
    # If it's a link being claimed by first view via button
    if not initial_share_check.get("recipient_id") and initial_share_check.get("recipient_type") == "link":
        set_on_view_fields["recipient_id"] = viewer_id
        set_on_view_fields["recipient_display_name"] = viewer_name
        LOGGER.info(f"Link share {initial_share_check['share_uuid']} (Token: {access_token}) being claimed by viewer {viewer_id} via button.")
    # Check for specific user assignment for button click
    elif initial_share_check.get("recipient_id") and initial_share_check.get("recipient_id") != viewer_id:
        await cb.answer("🚫 This secret is not intended for you.", show_alert=True)
        return

    # Atomically find active share, ensure view_count < max_views, then update status and increment view_count
    share = await shares_collection.find_one_and_update(
        filter={
            "access_token": access_token, 
            "status": "active",
            # Check max_views: expression ensures view_count is less than max_views
            # Only apply this check if max_views is a positive number (0 means unlimited)
            "$or": [
                {"max_views": {"$lte": 0}}, # max_views is 0 or negative (unlimited)
                {"$expr": {"$lt": ["$view_count", "$max_views"]}} # view_count < max_views
            ]
        },
        update={"$set": set_on_view_fields, "$inc": {"view_count": 1}},
        return_document=True # PyMongo: ReturnDocument.AFTER / Motor: True
    )

    if not share:
        # This means the share was not 'active', or max_views was reached by a concurrent request, or token invalid
        LOGGER.warning(f"Share (token {access_token}) not found for view by {viewer_id} or conditions not met during atomic update (e.g. already viewed/maxed).")
        # Re-fetch to give a more precise message if possible
        stale_share = await shares_collection.find_one({"access_token": access_token})
        if stale_share:
            if stale_share.get("status") != "active":
                 msg = f"⚠️ Secret already {stale_share['status']}."
            elif stale_share.get("max_views",1) > 0 and stale_share.get("view_count",0) >= stale_share.get("max_views",1):
                 msg = "⚠️ Secret has reached its maximum view limit."
            else:
                 msg = "⚠️ Secret unavailable or view conditions not met."
            await cb.answer(msg, show_alert=True)
        else: # Truly not found
            await cb.answer("⚠️ Secret no longer available.", show_alert=True)
        
        if cb.message: # Attempt to delete the (now invalid) button message
            try: await cb.message.delete()
            except: pass
        return

    # If we reached here, 'share' is the *updated* document
    await cb.answer("Secret unlocked! Revealing content now...", show_alert=False) # Quick feedback
    
    try:
        # Delete the "View Secret" button message itself FIRST.
        # This control message might have its own expiry timer associated with its job_id.
        button_message_id = cb.message.id
        button_chat_id = cb.message.chat.id # Should be viewer_id
        if cb.message:
            try:
                # Only delete the message if view_count < max_views and max_views > 0
                view_count = share.get("view_count", 0)
                max_views = share.get("max_views", 1)
                print(f"View count: {view_count}, Max views: {max_views}")
                # Only delete if: max_views > 0 AND view_count < max_views
                # Do NOT delete if: max_views == 0 (unlimited), or view_count >= max_views
                # Delete the button message if (max_views > 0 and view_count >= max_views)
                # Never delete if max_views == 0 (unlimited)
                if max_views > 0 and view_count >= max_views:
                    await cb.message.delete()
                    LOGGER.info(f"Deleted 'View Secret' button message {button_message_id} for share {share['share_uuid']}.")
                # If this button message had a timer, its job should ideally be cancelled too.
                # The job ID was `del_msg_{chat_id}_{message_id}_{share_uuid}`
                # If share['bot_message_id_to_recipient'] was indeed this cb.message.id
                if share.get('bot_message_id_to_recipient') == button_message_id and \
                   share.get('recipient_id') == button_chat_id:
                    job_id_btn_del = f"{JOB_ID_PREFIX_DELETE_MESSAGE}{button_chat_id}_{button_message_id}_{share['share_uuid']}"
                    if cancel_scheduled_job(job_id_btn_del):
                        LOGGER.info(f"Cancelled self-destruct job for 'View Secret' button {button_message_id}.")
            except Exception as e_del_btn:
                LOGGER.warning(f"Could not delete 'View Secret' button message {button_message_id}: {e_del_btn}")

        # Deliver the actual content
        source_chat_id = share["original_chat_id"]
        source_message_id = share["original_message_id"]
        send_kwargs = {"chat_id": viewer_id, "from_chat_id": source_chat_id, "message_id": source_message_id}

        if not share.get("show_forward_tag", True): # Sender chose hide tag
            if share.get("is_protected_content", False):
                 send_kwargs["protect_content"] = True
            await client.copy_message(**send_kwargs)
        else: # Show tag
            await client.forward_messages(
                chat_id=send_kwargs["chat_id"],
                from_chat_id=send_kwargs["from_chat_id"],
                message_ids=[send_kwargs["message_id"]]
            )
        
        LOGGER.info(f"Secret content {share['share_uuid']} delivered to button-click viewer {viewer_id}.")
        
        action_taken_message = "This secret has been viewed."

        # Check if this view was the last allowed view, and finalize status
        # 'share' document here IS the one after $inc, so its view_count is up-to-date
        new_view_count = share.get("view_count", 0) 
        share_max_views_limit = share.get("max_views", 1)

        if share_max_views_limit > 0 and new_view_count >= share_max_views_limit:
            LOGGER.info(f"Share {share['share_uuid']} reached max_views ({new_view_count}/{share_max_views_limit}) with this button click.")
            final_status_updates = {"status": "destructed"} # Mark as fully destructed
            if not share.get("destructed_at"): # Set only if not already (e.g. by timer job racing)
                final_status_updates["destructed_at"] = datetime.now(timezone.utc)
            await update_share(share["share_uuid"], final_status_updates)
            action_taken_message = "This secret has reached its view limit and is now destroyed."
            
            # Cleanup the temp message from "me" chat if it's an inline share that just got its final view
            if share.get("share_type") == "message_inline" and share.get("original_chat_id") == await client.get_me().id:
                try: await client.delete_messages(share["original_chat_id"], share["original_message_id"])
                except Exception as e_del_tmp: LOGGER.warning(f"Could not delete inline temp msg {share['original_message_id']} after final button view: {e_del_tmp}")


        # Notify sender (using the now up-to-date 'share' document)
        sender_id = share.get("sender_id")
        if sender_id and await get_user_setting(sender_id, "notify_on_view"):
            try:
                shared_with_text = f"user {share.get('recipient_display_name', viewer_name)}" \
                                   if share.get("recipient_type") == "user" else "link viewer (via button)"
                await client.send_message(
                    sender_id,
                    f"ℹ️ Your secret (ID: ...{share['share_uuid'][-6:]}, shared with {shared_with_text}) "
                    f"was just viewed by {viewer_name} (`{viewer_id}`). Status: {share.get('status')}."
                )
            except Exception as e_notify: 
                LOGGER.warning(f"Failed to send view notification for {share['share_uuid']} (button view): {e_notify}")
        
        # Cancel general link expiry if it was a link share and it just got destructed due to max views
        if share.get("recipient_type") == "link" and share.get("expires_at") and share.get("status") == "destructed":
            job_id_link_exp = f"{JOB_ID_PREFIX_EXPIRE_SHARE}{share['share_uuid']}"
            if cancel_scheduled_job(job_id_link_exp):
                 LOGGER.info(f"Cancelled link expiry job for {share['share_uuid']} as it was destructed by max_views.")
        
        # Optional: Send a small confirmation text to the viewer in chat AFTER content revealed.
        # await client.send_message(viewer_id, action_taken_message) 

    except Exception as e:
        LOGGER.exception(f"Error during secret delivery/finalization for {access_token} (button view) by {viewer_id}: {e}")
        # Don't cb.answer() again if already answered "Revealing content" unless it failed before that.
        # Bot has already ack'd the button. If delivery fails, viewer just doesn't get content.
        # We could try to send an error message to the viewer's chat:
        await client.send_message(viewer_id, "📛 An error occurred while trying to show you the secret content.")


# --- Inline Query Handler ---
@Client.on_inline_query(filters.regex(r"^(?!\s*$).+")) # Matches non-empty queries
@check_user_status # Inline queries also have from_user
async def inline_share_handler(client: Client, inline_query: InlineQuery):
    user_id = inline_query.from_user.id
    query_text = inline_query.query.strip()
    
    if not query_text: # Should be caught by regex but double check
        await inline_query.answer([], cache_time=5)
        return

    LOGGER.info(f"User {user_id} initiated inline query: '{query_text[:50]}...'")

    # Create a temporary message in bot's PM with itself to get message_id (Pyrogram trick)
    # This message will contain the text user wants to share.
    # print(inline_query)  # Avoid printing Pyrogram objects directly; use logging if needed
    try:
        temp_bot_message = await client.send_message(user_id, query_text)
    except Exception as e:
        LOGGER.error(f"Failed to send temporary message to self for inline query from {user_id}: {e}")
        # Optionally, inform user via a result that it failed, or just return no results.
        await inline_query.answer(
            results=[
                InlineQueryResultArticle(
                    title="Error Preparing Share",
                    description="Could not temporarily store your secret. Please try again or use normal share.",
                    input_message_content=InputTextMessageContent("Error creating inline secret.")
                )
            ],
            cache_time=10
        )
        return

    share_uuid = str(uuid.uuid4())
    access_token = str(uuid.uuid4()) # Different token for inline view
    
    # Get user's default protection settings
    user_prefs_show_tag = await get_user_setting(user_id, "default_show_forward_tag")
    user_prefs_protect_content = await get_user_setting(user_id, "default_protected_content")

    db_save_success = await save_inline_share_content(
        sender_id=user_id,
        text_content=query_text, # Or use temp_bot_message.text for consistency
        share_uuid=share_uuid,
        access_token=access_token,
        original_chat_id=temp_bot_message.chat.id, # "me"
        original_message_id=temp_bot_message.id,
        is_protected=user_prefs_protect_content,
        show_forward_tag=user_prefs_show_tag # For inline, "show_forward_tag:False" means copy content when viewed
    )

    if not db_save_success:
        LOGGER.error(f"Failed to save inline share content to DB for user {user_id}, share {share_uuid}.")
        await temp_bot_message.delete() # Clean up temp message
        # Inform user via result that DB save failed
        # ... (similar error result as above)
        return
    
    bot_username = client.me.username
    view_link = f"https://t.me/{bot_username}?start=viewsecret_{access_token}"

    results = [
        InlineQueryResultArticle(
            id=share_uuid, # Must be unique string
            title="🤫 Share this secret text",
            description=f"'{query_text[:30]}...' (One-time view link)",
            input_message_content=InputTextMessageContent(
                message_text=f"🤫 A secret has been shared with you!\n\n"
                             f"Click to view (one-time): {view_link}\n\n"
                             f"Shared by: {inline_query.from_user.mention}",
                disable_web_page_preview=True
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("👁️ View Secret (One-Time)", url=view_link)
            ]])
            # thumb_url="URL_TO_A_SECRET_ICON.PNG" # Optional: Icon for the result
        )
    ]
    
    try:
        await inline_query.answer(
            results=results,
            cache_time=config.INLINE_QUERY_CACHE_TIME, # Use configured cache time
            is_personal=True # Results are specific to this user's input
        )
        LOGGER.info(f"Sent inline result for share {share_uuid} to user {user_id}")
    except QueryIdInvalid:
        LOGGER.warning(f"Query ID invalid for inline query from {user_id}. User might have typed too fast or cleared.")
        await temp_bot_message.delete() # Clean up if answer fails critically
    except Exception as e:
        LOGGER.error(f"Error answering inline query for {user_id}: {e}")
        await temp_bot_message.delete()


# --- Generic Cancel Button Handler (ensure it's after specific prefix handlers if using general regex) ---
@Client.on_callback_query(filters.regex(f"^{SHARE_CANCEL_PREFIX}now:"))
@check_user_status
async def generic_share_cancel_handler(client: Client, cb: CallbackQuery):
    user_id = cb.from_user.id
    state, flow_data = get_user_state(user_id)
    
    # Try to get share_uuid from callback data first, then from state
    cb_share_uuid = None
    try: cb_share_uuid = cb.data.split(":",2)[2]
    except IndexError: pass

    active_flow_data = None
    if cb_share_uuid and flow_data.get("share_uuid") == cb_share_uuid:
        active_flow_data = flow_data
    elif not cb_share_uuid and "share_uuid" in flow_data and state != UserState.DEFAULT: # Fallback if no UUID in cb data but user is in a flow
        active_flow_data = flow_data
    else: # Mismatch or no flow data for this user
        LOGGER.warning(f"User {user_id} tried to cancel, but no matching share flow found. Callback UUID: {cb_share_uuid}, State data: {flow_data}")
        await cb.answer("No active share process to cancel or session expired.", show_alert=True)
        # If a message was edited, send a new main menu
        if cb.message: await send_main_menu(client, user_id, cb.message, edit=False)
        return

    await cancel_current_share_flow(client, user_id, cb, active_flow_data)