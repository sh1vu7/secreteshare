import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, User as PyrogramUser
from pyrogram.errors import FloodWait, UserIsBlocked, PeerIdInvalid, ListenerTimeout, BadRequest, UserAdminInvalid, ChatAdminRequired, MessageNotModified

import config
from db import (
    get_user, update_user_details, users_collection, get_all_user_ids,
    shares_collection, add_user # add_user in case admin tries to manage a non-existent user first time
)
from utils.keyboards import (
    create_admin_panel_keyboard, create_admin_user_management_keyboard,
    ADMIN_PANEL_CALLBACK, ADMIN_USERS_CALLBACK, ADMIN_BROADCAST_CALLBACK,
    ADMIN_STATS_CALLBACK, ADMIN_PROMOTE_SUDO_PREFIX, ADMIN_DEMOTE_SUDO_PREFIX,
    ADMIN_GRANT_PREMIUM_PREFIX, ADMIN_REVOKE_PREMIUM_PREFIX,
    ADMIN_BAN_USER_PREFIX, ADMIN_UNBAN_USER_PREFIX, MAIN_MENU_CALLBACK
)
from utils.decorators import check_user_status, sudo_users_only, owner_only
from utils.user_states import UserState, set_user_state, get_user_state, clear_user_state
from handlers.start_help import send_main_menu # For navigation

LOGGER = logging.getLogger(__name__)
BROADCAST_ASK_TIMEOUT = 600 # 10 minutes for broadcast message

async def display_user_management_panel(client: Client, admin_message: Message, target_user_id_int: int):
    target_pyrogram_user: Optional[PyrogramUser] = None
    try:
        target_pyrogram_user = await client.get_users(target_user_id_int)
    except PeerIdInvalid:
        await admin_message.reply_text(f"Cannot fetch live Telegram profile for User ID {target_user_id_int}. User may not exist or bot can't see them. Showing DB data only.")
    except Exception as e:
        LOGGER.warning(f"Error fetching pyrogram user {target_user_id_int} for admin panel: {e}")

    target_user_db = await get_user(target_user_id_int)
    if not target_user_db: # If user is not in DB at all yet
        if target_pyrogram_user: # We found them on TG, so add them
            target_user_db = await add_user(
                target_user_id_int,
                first_name=target_pyrogram_user.first_name,
                username=target_pyrogram_user.username
            )
            await admin_message.reply_text(f"User {target_pyrogram_user.first_name or target_user_id_int} was not in DB, added now. Proceed with management.")
        else: # Cannot find on TG and not in DB
            await admin_message.reply_text(f"User ID {target_user_id_int} not found in bot's database and could not be fetched from Telegram. They need to /start the bot first.")
            return

    display_name = target_user_db.get("first_name") or \
                   (f"@{target_user_db.get('username')}" if target_user_db.get('username') else None) or \
                   (target_pyrogram_user.first_name if target_pyrogram_user else None) or \
                   f"User ID {target_user_db['user_id']}"

    is_target_owner = target_user_db['user_id'] == config.OWNER_ID
    is_target_self = admin_message.from_user.id == target_user_db['user_id']

    keyboard = create_admin_user_management_keyboard(
        user_id=target_user_db['user_id'],
        current_role=target_user_db['role'],
        is_banned=target_user_db.get('banned', False),
        # Pass `is_target_owner` to keyboard create function instead of `user_is_owner`
        user_is_owner=is_target_owner
    )

    text = f"👤 Managing User: **{display_name}** (`{target_user_db['user_id']}`)\n"
    text += f"   Role: `{target_user_db['role']}`\n"
    text += f"   Premium: `{'Yes' if target_user_db.get('is_premium') else 'No'}`"
    if target_user_db.get('premium_expiry'):
        text += f" (Expires: {target_user_db['premium_expiry']:%Y-%m-%d %H:%M} UTC)\n"
    else:
        text += "\n"
    text += f"   Sudo (DB): `{'Yes' if target_user_db.get('is_sudo') else 'No'}`\n"
    text += f"   Sudo (Config): `{'Yes' if target_user_db['user_id'] in config.SUDO_USERS else 'No'}`\n"
    text += f"   Banned: `{'Yes' if target_user_db.get('banned') else 'No'}`"
    if target_user_db.get('banned_reason'): text += f" (Reason: {target_user_db['banned_reason']})\n"
    else: text += "\n"
    text += f"   Shares Count: `{target_user_db.get('shares_count', 0)}`\n"
    text += f"   First Seen: `{target_user_db.get('first_seen', 'N/A').strftime('%Y-%m-%d') if isinstance(target_user_db.get('first_seen'), datetime) else 'N/A'}`\n"
    text += f"   Last Active: `{target_user_db.get('last_active', 'N/A').strftime('%Y-%m-%d %H:%M') if isinstance(target_user_db.get('last_active'), datetime) else 'N/A'} UTC`\n\n"

    if is_target_owner: text += "ℹ️ _Owner status cannot be modified here (except granting explicit premium status if not already)._\n"
    if is_target_self: text += "ℹ️ _You are managing yourself. Some actions may be restricted._\n"
    text += "Select an action:"
    await admin_message.reply_text(text, reply_markup=keyboard)


@Client.on_callback_query(filters.regex(f"^{ADMIN_PANEL_CALLBACK}$"))
@check_user_status
@sudo_users_only # Decorator from utils.decorators checks if user is in config.SUDO_USERS
async def admin_panel_entry_handler(client: Client, cb: CallbackQuery):
    user_id = cb.from_user.id
    LOGGER.info(f"Admin panel accessed by Sudo User {user_id}")
    keyboard = create_admin_panel_keyboard()
    try:
        await cb.edit_message_text("👑 **Admin Panel**\n\nSelect an action:", reply_markup=keyboard)
        await cb.answer()
    except Exception as e:
        LOGGER.error(f"Error displaying admin panel for {user_id}: {e}")
        await cb.answer("Error loading admin panel.", show_alert=True)

@Client.on_callback_query(filters.regex(f"^{ADMIN_USERS_CALLBACK}$"))
@check_user_status
@sudo_users_only
async def admin_manage_users_prompt_handler(client: Client, cb: CallbackQuery):
    admin_user_id = cb.from_user.id
    # clear_user_state(admin_user_id) # Not strictly needed before an ask, but can ensure clean slate if there were other states

    prompt_text = ("👥 **Manage Users**\n\n"
                   "Send me the User ID, @username of the user to manage.\n\n"
                   "This request will time out in 5 minutes. You can type /cancel to abort.")
    
    # Using a simple cancel instruction via text (/cancel) is easier than a callback here
    # as cancelling client.ask via an external callback is complex.
    #await cb.edit_message_text(prompt_text, reply_markup=None) # Remove previous buttons if any
    await cb.answer("Waiting for user details...")

    try:

        user_details_message: Message = await client.ask(
            text=prompt_text,
            chat_id=admin_user_id,
            # No 'text' needed in ask() itself if prompt sent separately.
            # If you want ask() to send the prompt, pass text=... here instead of send_message above.
            filters=(filters.text | filters.forwarded) & filters.private & ~filters.command(["start", "help"]), # Allow /cancel to be processed
            timeout=300  # 5 minutes
        )

        # Clean up the bot's "⏳ Waiting..." prompt message
        #try: await cb.delete()
        #except: pass


        # --- Process the received user_details_message ---
        if user_details_message.text and user_details_message.text.lower() == "/cancel":
            await user_details_message.reply_text("User management cancelled.")
            # Return to admin panel
            keyboard = create_admin_panel_keyboard()
            await user_details_message.reply_text("👑 **Admin Panel**", reply_markup=keyboard)
            return

        target_user_id_int: Optional[int] = None

        if user_details_message.text:
            target_user_input = user_details_message.text.strip()
            if target_user_input.startswith("@"):
                try:
                    fetched_user = await client.get_users(target_user_input)
                    if fetched_user: target_user_id_int = fetched_user.id
                except PeerIdInvalid:
                    await user_details_message.reply_text(f"Username {target_user_input} not found.")
                    # keyboard = create_admin_panel_keyboard() # Option to go back or let admin try again implicitly
                    # await user_details_message.reply_text("👑 **Admin Panel**", reply_markup=keyboard)
                    return # Or re-prompt? For now, just stop.
                except Exception as e:
                    await user_details_message.reply_text(f"Error fetching {target_user_input}: {e}")
                    return
            elif target_user_input.isdigit():
                try:
                    target_user_id_int = int(target_user_input)
                except ValueError:
                     await user_details_message.reply_text(f"Invalid User ID format: {target_user_input}")
                     return
            else:
                await user_details_message.reply_text("Invalid input format. Send User ID, @username, or forward a message.")
                return
        else: # Should not be reached if filters are correct
            await user_details_message.reply_text("Unsupported message type for identifying the target user.")
            return
            
        if not target_user_id_int:
            await user_details_message.reply_text("Could not identify target user. Please try again.")
            # Show admin panel again or re-prompt for user
            # keyboard = create_admin_panel_keyboard()
            # await user_details_message.reply_text("👑 **Admin Panel**", reply_markup=keyboard)
            return

        # Call the helper function to display the management panel
        # Pass user_details_message as `admin_message` so it replies to the admin's input message.
        await display_user_management_panel(client, user_details_message, target_user_id_int)

    except ListenerTimeout:
        # If the bot was waiting via ask_message_prompt, try to delete that specific message.
        # We'd need to store its ID if we want to delete it reliably here.
        # For simplicity, just send a timeout message.
        await client.send_message(admin_user_id, "⏰ Timed out waiting for user details. Manage users action cancelled.")
        keyboard = create_admin_panel_keyboard()
        await client.send_message(admin_user_id, "👑 **Admin Panel**", reply_markup=keyboard)
    except Exception as e:
        LOGGER.error(f"Error in admin 'Manage Users' (ask flow) for admin {admin_user_id}: {e}")
        await client.send_message(admin_user_id, "An unexpected error occurred during user management setup. Please try again.")
        keyboard = create_admin_panel_keyboard()
        await client.send_message(admin_user_id, "👑 **Admin Panel**", reply_markup=keyboard)
    # finally:
        # clear_user_state(admin_user_id) # No specific state was set for this 'ask'

# Corrected logic structure for admin_receive_user_to_manage_handler
# (Assuming this handler is still active and not replaced by the client.ask method I described earlier)

#@Client.on_message(filters.private & (filters.text | filters.forwarded) & ~filters.via_bot)
#@check_user_status
#@sudo_users_only
async def admin_receive_user_to_manage_handler(client: Client, message: Message):
    admin_user_id = message.from_user.id # This is the admin performing the action

    # --- Condition to check if this message is intended for this handler ---
    # This part is crucial. Without client.ask, you need a way to know if this
    # specific message from the admin is meant to specify a user for management.
    # Example: using a UserState (preferred if not using client.ask here).
    # current_admin_state, _ = get_user_state(admin_user_id)
    # if current_admin_state != UserState.AWAITING_ADMIN_TARGET_USER_INPUT:
    #     return # Not expecting this input now

    # Or, less reliably, checking if it's a reply to the bot's prompt.
    is_reply_to_manage_prompt = False
    if message.reply_to_message and message.reply_to_message.from_user.is_self:
        if "Send me the User ID" in message.reply_to_message.text or "Manage Users" in message.reply_to_message.text:
             is_reply_to_manage_prompt = True
    
    # if not is_reply_to_manage_prompt AND current_admin_state != UserState.AWAITING_ADMIN_TARGET_USER_INPUT:
    #    return # Not for us right now

        target_user_id_int: Optional[int] = None
    # target_pyrogram_user_obj is mostly for getting the ID initially; display_user_management_panel will refetch

    # 1. Check for forwarded message first
        if message.from_user:
            target_user_id_int = message.from_user.id
        else:
            # Forwarded from channel or hidden user, not a direct user to manage
            await message.reply_text("Cannot manage users forwarded from channels or anonymous sources this way. Please forward from a user directly or provide ID/@username.")
            return
    # 2. Else, check if it's a text message (ID or @username)
    elif message.text:
        target_user_input = message.text.strip()
        if target_user_input.startswith("@"):
            try:
                user_obj = await client.get_users(target_user_input)
                if user_obj: target_user_id_int = user_obj.id
            except PeerIdInvalid:
                await message.reply_text(f"Username {target_user_input} not found.")
                return
            except Exception as e:
                await message.reply_text(f"Error fetching {target_user_input}: {e}")
                return
        elif target_user_input.isdigit():
            try:
                target_user_id_int = int(target_user_input)
            except ValueError:
                await message.reply_text(f"Invalid User ID format: {target_user_input}")
                return
        else:
            # Not a forward, not @username, not User ID.
            # Only proceed if it's a reply to a prompt and user likely made a mistake.
            # Otherwise, ignore as it could be a casual message from admin.
            if is_reply_to_manage_prompt: # Or if admin_state was AWAITING_ADMIN_TARGET_USER_INPUT
                await message.reply_text("Invalid input. Send User ID, @username, or forward a message from the target user.")
            return # Silently ignore if not clearly intended for this handler
    else:
        # Neither forwarded nor text. This case should typically be filtered out by Pyrogram filters,
        # but if it somehow gets here, ignore or reply with error if expecting input.
        if is_reply_to_manage_prompt: # Or if admin_state was AWAITING_ADMIN_TARGET_USER_INPUT
             await message.reply_text("Unsupported message type received for user management.")
        return

    if not target_user_id_int:
        # This means parsing failed or input was invalid but not caught by specific error messages above.
        await message.reply_text("Could not identify a target user from your message.")
        return
    
    # clear_user_state(admin_user_id) # Clear state if you were using one for AWAITING_ADMIN_TARGET_USER_INPUT
    await display_user_management_panel(client, message, target_user_id_int)


@Client.on_callback_query(
    filters.regex(f"^({ADMIN_PROMOTE_SUDO_PREFIX}|{ADMIN_DEMOTE_SUDO_PREFIX}|{ADMIN_GRANT_PREMIUM_PREFIX}|{ADMIN_REVOKE_PREMIUM_PREFIX}|{ADMIN_BAN_USER_PREFIX}|{ADMIN_UNBAN_USER_PREFIX})")
)
@check_user_status # Admin performing action must be valid
@sudo_users_only # Admin must be sudo to perform these actions
async def admin_user_action_handler(client: Client, cb: CallbackQuery):
    admin_user_id = cb.from_user.id
    action_prefix = cb.data.split(":")[0] + ":" # e.g., "admin_p_sudo:"
    target_user_id = int(cb.data.split(":",1)[1]) # The rest is user_id

    target_user_db = await get_user(target_user_id)
    if not target_user_db:
        await cb.answer("Target user not found in DB.", show_alert=True); return

    is_target_owner = target_user_id == config.OWNER_ID
    is_admin_owner = admin_user_id == config.OWNER_ID
    action_taken_message = ""
    requires_owner = [ADMIN_PROMOTE_SUDO_PREFIX, ADMIN_DEMOTE_SUDO_PREFIX]

    if action_prefix in requires_owner and not is_admin_owner:
        await cb.answer("🚫 Only Bot Owner can manage Sudo status.", show_alert=True); return
    
    if is_target_owner and action_prefix not in [ADMIN_GRANT_PREMIUM_PREFIX, ADMIN_REVOKE_PREMIUM_PREFIX]: # Owner status largely immutable here
        await cb.answer("🚫 Owner's role/ban status cannot be changed via this panel.", show_alert=True); return

    if target_user_id == admin_user_id and action_prefix in [ADMIN_BAN_USER_PREFIX, ADMIN_DEMOTE_SUDO_PREFIX]: # Cannot ban/demote self
        await cb.answer("🚫 You cannot perform this action on yourself.", show_alert=True); return


    success = False
    updates = {}

    if action_prefix == ADMIN_PROMOTE_SUDO_PREFIX:
        if target_user_db['role'] == "sudo": action_taken_message = "User is already Sudo."
        else: updates = {"role": "sudo", "is_sudo": True, "is_premium": True}; success=True; action_taken_message="User promoted to Sudo."
    elif action_prefix == ADMIN_DEMOTE_SUDO_PREFIX:
        if target_user_db['role'] != "sudo": action_taken_message = "User is not Sudo."
        # Demoting from sudo: if they had explicit premium before, restore? Or set to free?
        # Current db.update_user_details handles is_premium=False if role becomes free
        # Here we assume demote to "free" unless they have premium_expiry.
        # Let's simplify: demote to free, premium status will be based on premium_expiry or if they were 'premium' role.
        else: updates = {"role": "free", "is_sudo": False}; success=True; action_taken_message = "User demoted from Sudo to Free."
              # If user had explicit premium flag or expiry, it might remain or be re-evaluated by get_user()
    elif action_prefix == ADMIN_GRANT_PREMIUM_PREFIX:
        updates = {"is_premium": True, "premium_expiry": datetime.now(timezone.utc) + timedelta(days=30*100)} # Grant "lifetime" effectively or ask for duration
        if target_user_db['role'] == "free": updates["role"] = "premium" # Elevate role if they were free
        success=True; action_taken_message = "Premium granted."
    elif action_prefix == ADMIN_REVOKE_PREMIUM_PREFIX:
        updates = {"is_premium": False, "premium_expiry": None}
        if target_user_db['role'] == "premium": updates["role"] = "free" # Revert role if they were 'premium'
        success=True; action_taken_message = "Premium revoked."
    elif action_prefix == ADMIN_BAN_USER_PREFIX:
        if target_user_db.get('banned'): action_taken_message = "User already banned."
        else: updates = {"banned": True, "ban_reason": f"Banned by admin {admin_user_id}"}; success=True; action_taken_message="User banned."
    elif action_prefix == ADMIN_UNBAN_USER_PREFIX:
        if not target_user_db.get('banned'): action_taken_message = "User not banned."
        else: updates = {"banned": False, "ban_reason": None}; success=True; action_taken_message="User unbanned."

    if success and updates:
        db_op_success = await update_user_details(target_user_id, updates)
        if not db_op_success:
            action_taken_message = "DB update failed."
            success = False # Revert success flag
        else:
            LOGGER.info(f"Admin {admin_user_id} changed {target_user_id}: {action_prefix} -> {updates}")

    await cb.answer(action_taken_message, show_alert=True)

    if success: # Refresh panel
        await display_user_management_panel(client, cb.message, target_user_id)
        # Since display_user_management_panel sends a new message or replies,
        # we might want to delete the message that had the buttons just clicked.
        try: await cb.message.edit_reply_markup(None) # Remove buttons from old message
        except: pass


# In handlers/admin_panel.py

# DELETE or COMMENT OUT the following three functions:
# - admin_broadcast_prompt_handler (the old one)
# - admin_receive_broadcast_content_handler
# - admin_broadcast_confirm_action_handler

# NEW Combined Handler using client.ask:
@Client.on_callback_query(filters.regex(f"^{ADMIN_BROADCAST_CALLBACK}$"))
@check_user_status
@sudo_users_only
async def admin_broadcast_handler(client: Client, cb: CallbackQuery): # Renamed for clarity
    admin_user_id = cb.from_user.id
    # clear_user_state(admin_user_id) # Not strictly necessary before ask for this simple case

    prompt_text = ("📢 **Broadcast Message**\n\n"
                   "Send the message you want to broadcast (text, photo, document, etc.).\n"
                   "It will be copied to all non-banned users. Use Markdown for text.\n\n"
                   "This request will time out in 10 minutes. Type /cancelbroadcast to abort.")

    #await cb.edit_message_text(prompt_text, reply_markup=None) # Remove old buttons
    await cb.answer("Waiting for broadcast content...")

    try:
        # Bot sends a clear "waiting" message
        # ask_prompt_msg = await client.send_message(
        #     chat_id=admin_user_id,
        #     text="⏳ _Please send the content you wish to broadcast now... (/cancelbroadcast to abort)_"
        # )

        broadcast_content_message: Message = await client.ask(
            text=prompt_text,
            chat_id=admin_user_id,
            filters=~filters.command(["start", "help"]) & filters.private, # Allow /cancelbroadcast, any content
            timeout=BROADCAST_ASK_TIMEOUT 
        )

        # # Clean up the bot's "⏳ Waiting..." prompt
        # try: await ask_prompt_msg.delete()
        # except: pass

        if broadcast_content_message.text and broadcast_content_message.text.lower() == "/cancelbroadcast":
            await broadcast_content_message.reply_text("Broadcast cancelled.")
            keyboard = create_admin_panel_keyboard()
            await broadcast_content_message.reply_text("👑 **Admin Panel**", reply_markup=keyboard)
            return

        # --- Confirmation Step ---
        # For text messages, show a preview. For media, just confirm type.
        content_preview_text = "your message"
        if broadcast_content_message.text:
            content_preview_text = f"the following text:\n\n\"_{broadcast_content_message.text[:200]}{'...' if len(broadcast_content_message.text) > 200 else ''}_\""
        elif broadcast_content_message.media:
            media_type = broadcast_content_message.media.value # e.g., "photo", "video"
            file_name_str = ""
            if hasattr(broadcast_content_message, str(media_type)) and \
               hasattr(getattr(broadcast_content_message, str(media_type)), "file_name") and \
               getattr(getattr(broadcast_content_message, str(media_type)), "file_name"):
                file_name_str = f" (Name: {getattr(getattr(broadcast_content_message, str(media_type)), 'file_name')})"

            content_preview_text = f"a **{media_type.replace('_', ' ').title()}**{file_name_str}"
        
        confirmation_prompt_text = (f"Are you sure you want to broadcast {content_preview_text} "
                                    f"to all non-banned users?\n\nThis action cannot be undone.")

        confirm_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Yes, Broadcast", callback_data=f"admin_bcast_exec:yes"),
                InlineKeyboardButton("❌ No, Cancel", callback_data=f"admin_bcast_exec:no")
            ]
        ])
        
        # Store message details needed for broadcast execution in a temporary state for the confirmation callback
        set_user_state(admin_user_id, UserState.AWAITING_CONFIRMATION, { # Re-using AWAITING_CONFIRMATION for this
            "broadcast_content_msg_id": broadcast_content_message.id,
            "broadcast_content_chat_id": broadcast_content_message.chat.id
        })

        await broadcast_content_message.reply_text(confirmation_prompt_text, reply_markup=confirm_kb)

    except ListenerTimeout:
        await client.send_message(admin_user_id, "⏰ Timed out waiting for broadcast content. Action cancelled.")
        keyboard = create_admin_panel_keyboard()
        await client.send_message(admin_user_id, "👑 **Admin Panel**", reply_markup=keyboard)
    except Exception as e:
        LOGGER.error(f"Error in admin broadcast (ask content flow) for {admin_user_id}: {e}")
        await client.send_message(admin_user_id, "An unexpected error occurred. Broadcast cancelled.")
        keyboard = create_admin_panel_keyboard()
        await client.send_message(admin_user_id, "👑 **Admin Panel**", reply_markup=keyboard)
    # finally:
        # No specific state was set just for `ask`, so clear_user_state() isn't critical here
        # unless you add one, e.g., UserState.AWAITING_BROADCAST_CONTENT_VIA_ASK

# New handler for the confirmation callback from above
@Client.on_callback_query(filters.regex("^admin_bcast_exec:"))
@check_user_status
@sudo_users_only
async def admin_broadcast_execute_handler(client: Client, cb: CallbackQuery):
    admin_user_id = cb.from_user.id
    action = cb.data.split(":")[1] # yes or no

    state, temp_data = get_user_state(admin_user_id)

    if state != UserState.AWAITING_CONFIRMATION or "broadcast_content_msg_id" not in temp_data:
        await cb.answer("Session error or broadcast data missing for confirmation.", show_alert=True)
        clear_user_state(admin_user_id)
        try:
            await cb.message.edit_text("Confirmation failed. Please restart broadcast from Admin Panel.",
                                       reply_markup=create_admin_panel_keyboard())
        except: pass # If message edit fails
        return

    broadcast_msg_id = temp_data["broadcast_content_msg_id"]
    broadcast_chat_id = temp_data["broadcast_content_chat_id"]
    clear_user_state(admin_user_id) # State processed

    if action == "no":
        await cb.edit_message_text("Broadcast cancelled by admin decision.")
        await cb.answer("Broadcast cancelled.")
        await cb.message.reply_text("👑 **Admin Panel**", reply_markup=create_admin_panel_keyboard())
        return

    # --- Proceed with Actual Broadcast Execution (action == "yes") ---
    await cb.edit_message_text("📢 Broadcasting in progress... This may take a moment. You will receive a final status update.", reply_markup=None)
    await cb.answer("Broadcast started...")

    all_target_user_ids = await get_all_user_ids(include_banned=False)
    if not all_target_user_ids:
        await cb.message.reply_text("No users (excluding banned) to broadcast to.", 
                                   reply_markup=create_admin_panel_keyboard())
        return

    sent_count, failed_count = 0, 0
    total_to_send = len(all_target_user_ids)
    # Send progress as a new message to avoid issues with editing the callback message rapidly
    progress_update_msg = await client.send_message(admin_user_id, f"Broadcast starting... Target: {total_to_send} users.")
    last_update_time = datetime.now(timezone.utc)

    for i, target_uid in enumerate(all_target_user_ids):
        if target_uid == admin_user_id: # Don't broadcast to self
            continue
        try:
            await client.copy_message(
                chat_id=target_uid,
                from_chat_id=broadcast_chat_id, # Admin's chat with bot
                message_id=broadcast_msg_id      # The message admin sent as content
            )
            sent_count += 1
        except UserIsBlocked: failed_count += 1; LOGGER.warning(f"Broadcast: User {target_uid} blocked bot.")
        except PeerIdInvalid: failed_count += 1; LOGGER.warning(f"Broadcast: User {target_uid} ID invalid.")
        except FloodWait as e_flood:
            LOGGER.warning(f"Broadcast FloodWait: sleeping for {e_flood.value}s.")
            try:
                await progress_update_msg.edit_text(
                    f"Hit FloodWait. Pausing for {e_flood.value}s...\n"
                    f"Progress: Sent {sent_count}, Failed {failed_count} / Total {total_to_send}"
                )
            except: pass # Ignore if edit fails during floodwait
            await asyncio.sleep(e_flood.value + 2) # Sleep for specified time + buffer
            try: # Retry after sleep
                await client.copy_message(target_uid, broadcast_chat_id, broadcast_msg_id)
                sent_count += 1
            except Exception as e_retry:
                failed_count += 1; LOGGER.error(f"Broadcast retry failed for {target_uid} after FloodWait: {e_retry}")
        except Exception as e_send_err:
            failed_count += 1; LOGGER.error(f"Broadcast error for user {target_uid}: {e_send_err}")
        
        # Update progress message periodically
        current_time = datetime.now(timezone.utc)
        if (current_time - last_update_time).total_seconds() > 5 or (i % 25 == 0 and i > 0) or (i == total_to_send -1) :
            try:
                await progress_update_msg.edit_text(
                    f"Broadcasting...\nSent: {sent_count}, Failed: {failed_count} / Total {total_to_send}"
                )
                last_update_time = current_time
            except FloodWait as e_edit_flood: await asyncio.sleep(e_edit_flood.value + 1) # Sleep if edit is flooded
            except Exception as e_edit_prog: LOGGER.warning(f"Failed to edit broadcast progress: {e_edit_prog}")
        
        await asyncio.sleep(0.05) # 50ms delay between sends to be gentle (Telegram allows 30msg/sec to different users)

    final_summary_text = (
        f"📢 **Broadcast Complete!**\n\n"
        f"Successfully sent to: {sent_count} users\n"
        f"Failed to send to: {failed_count} users\n"
        f"Total users attempted: {total_to_send}"
    )
    try: await progress_update_msg.edit_text(final_summary_text)
    except: await client.send_message(admin_user_id, final_summary_text) # Send as new if edit fails

    LOGGER.info(f"Broadcast by admin {admin_user_id} finished. Sent: {sent_count}, Failed: {failed_count}.")
    await client.send_message(admin_user_id, "Return to Admin Panel:", reply_markup=create_admin_panel_keyboard())


# @Client.on_message(filters.private & ~filters.command(["start", "help"])) # Handle any message type, and /cancelbroadcast
# @check_user_status
# @sudo_users_only
# async def admin_receive_broadcast_content_handler(client: Client, message: Message):
#     admin_user_id = message.from_user.id
#     state, _ = get_user_state(admin_user_id)

#     if state != UserState.AWAITING_BROADCAST_MESSAGE:
#         # If admin sends a message not related to broadcast and they are not in broadcast state, ignore.
#         # This assumes other handlers for sudo users (like manage user by ID) take precedence or are filtered out.
#         return

#     if message.text and message.text.lower() == "/cancelbroadcast":
#         clear_user_state(admin_user_id)
#         await message.reply_text("Broadcast cancelled.")
#         # Send back to admin panel using send_main_menu (as a hack, or create specific func)
#         await admin_panel_entry_handler(client, message) # This expects a CallbackQuery...
#                                                         # better to call a func that sends the admin panel menu directly.
#                                                         # For now, sending a simple text with kb:
#         await message.reply_text("Admin Panel:", reply_markup=create_admin_panel_keyboard())

#         return

#     broadcast_content_message = message # The message itself is the content
#     clear_user_state(admin_user_id)

#     confirm_text = "Message received for broadcast. This will copy the message to all users. Proceed?"
#     confirm_kb = InlineKeyboardMarkup([
#         [InlineKeyboardButton("✅ Yes, Broadcast Now", callback_data="admin_broadcast_confirm:yes")],
#         [InlineKeyboardButton("❌ No, Cancel Broadcast", callback_data="admin_broadcast_confirm:no")]
#     ])
#     # Store message_id to be broadcast in user state temporarily, or pass via callback.
#     # For simplicity, let's use another state transition. Not ideal for just one message_id.
#     # Alternative: Callback like "admin_bconfirm:yes:chat_id:msg_id". Complex for diverse content.
#     # Storing in state (short-lived):
#     set_user_state(admin_user_id, UserState.AWAITING_CONFIRMATION, {"broadcast_message_id": broadcast_content_message.id, "broadcast_chat_id": broadcast_content_message.chat.id})
#     await message.reply_text(confirm_text, reply_markup=confirm_kb)

# @Client.on_callback_query(filters.regex("^admin_broadcast_confirm:"))
# @check_user_status
# @sudo_users_only
# async def admin_broadcast_confirm_action_handler(client: Client, cb: CallbackQuery):
#     admin_user_id = cb.from_user.id
#     state, data = get_user_state(admin_user_id)
#     action = cb.data.split(":")[1]

#     if state != UserState.AWAITING_CONFIRMATION or "broadcast_message_id" not in data:
#         await cb.answer("Session error or broadcast data missing.", show_alert=True)
#         clear_user_state(admin_user_id) # Clear broken state
#         await cb.message.edit_text("Broadcast confirmation failed. Please start over from Admin Panel.", reply_markup=create_admin_panel_keyboard())
#         return

#     original_broadcast_message_id = data["broadcast_message_id"]
#     original_broadcast_chat_id = data["broadcast_chat_id"]
#     clear_user_state(admin_user_id) # Clear state after getting data

#     if action == "no":
#         await cb.edit_message_text("Broadcast cancelled by admin.")
#         # Optionally delete the stored message if it was, e.g. bot forwarded it to itself.
#         # Here, admin sent it directly, so no cleanup of content message itself.
#         await cb.answer("Broadcast cancelled.")
#         await cb.message.reply_text("Admin Panel:", reply_markup=create_admin_panel_keyboard())
#         return

#     # --- Proceed with Broadcast ---
#     await cb.edit_message_text("📢 Broadcasting... This may take a while. Status updates will follow.")
#     await cb.answer("Starting broadcast...")

#     all_user_ids_to_send = await get_all_user_ids(include_banned=False)
#     if not all_user_ids_to_send:
#         await cb.message.reply_text("No users (excluding banned) found to broadcast to.")
#         return

#     sent_count, failed_count = 0, 0
#     total_users = len(all_user_ids_to_send)
#     progress_msg = await cb.message.reply_text(f"Sent: 0, Failed: 0 / Total: {total_users}")
#     last_update_time = datetime.now(timezone.utc)

#     for i, user_id_target in enumerate(all_user_ids_to_send):
#         if user_id_target == admin_user_id: continue # Skip self
#         try:
#             # Copy the original message sent by admin
#             await client.copy_message(
#                 chat_id=user_id_target,
#                 from_chat_id=original_broadcast_chat_id,
#                 message_id=original_broadcast_message_id
#             )
#             sent_count += 1
#         except UserIsBlocked: failed_count += 1; LOGGER.warning(f"Broadcast: User {user_id_target} blocked bot.")
#         except PeerIdInvalid: failed_count += 1; LOGGER.warning(f"Broadcast: User {user_id_target} invalid peer.")
#         except FloodWait as e:
#             LOGGER.warning(f"Broadcast FloodWait: sleeping {e.value}s.")
#             await progress_msg.edit_text(f"FloodWait for {e.value}s... Sent: {sent_count}, Failed: {failed_count}")
#             await asyncio.sleep(e.value + 2)
#             # Retry current user
#             try:
#                 await client.copy_message(user_id_target, original_broadcast_chat_id, original_broadcast_message_id)
#                 sent_count +=1
#             except Exception as er:
#                 failed_count+=1; LOGGER.error(f"Broadcast retry failed for {user_id_target}: {er}")
#         except Exception as e_send:
#             failed_count += 1; LOGGER.error(f"Broadcast error for user {user_id_target}: {e_send}")
        
#         if (datetime.now(timezone.utc) - last_update_time).total_seconds() > 5 or (i % 50 == 0 and i > 0) or i == total_users -1 :
#             try:
#                 await progress_msg.edit_text(f"Sent: {sent_count}, Failed: {failed_count} / Total: {total_users}")
#                 last_update_time = datetime.now(timezone.utc)
#             except FloodWait as e_edit: await asyncio.sleep(e_edit.value + 1)
#             except Exception: pass # Ignore edit errors, focus on sending
#         await asyncio.sleep(0.1) # 100ms delay

#     summary = f"📢 **Broadcast Complete**\nSent: {sent_count}\nFailed: {failed_count}\nTotal: {total_users}"
#     await progress_msg.edit_text(summary)
#     LOGGER.info(f"Broadcast by {admin_user_id} finished. Sent: {sent_count}, Failed: {failed_count}")
#     # await cb.message.reply_text("Admin Panel:", reply_markup=create_admin_panel_keyboard())


@Client.on_callback_query(filters.regex(f"^{ADMIN_STATS_CALLBACK}$"))
@check_user_status
@sudo_users_only
async def admin_stats_handler(client: Client, cb: CallbackQuery):
    total_users = await users_collection.count_documents({})
    banned = await users_collection.count_documents({"banned": True})
    sudo_db = await users_collection.count_documents({"is_sudo": True}) # Counts based on DB flag
    premium_db = await users_collection.count_documents({"is_premium": True})
    
    # Share stats
    total_s = await shares_collection.count_documents({})
    active_s = await shares_collection.count_documents({"status": "active"})
    viewed_s = await shares_collection.count_documents({"status": "viewed"})
    expired_s = await shares_collection.count_documents({"status": {"$in": ["expired", "destructed", "revoked"]}})

    stats_text = f"""📊 **Bot Statistics** ({datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC)

**Users:**
▫️ Total Users: `{total_users}`
▫️ Banned Users: `{banned}`
▫️ Sudo Users (DB flag): `{sudo_db}`
▫️ Sudo Users (Config): `{len(config.SUDO_USERS)}`
▫️ Premium Users (DB flag): `{premium_db}`

**Shares:**
▫️ Total Shares: `{total_s}`
▫️ Active Shares: `{active_s}`
▫️ Viewed Shares: `{viewed_s}`
▫️ Finalized (Expired/Destructed/Revoked): `{expired_s}`
"""
    back_button = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Admin Panel", callback_data=ADMIN_PANEL_CALLBACK)]])
    try:
        await cb.edit_message_text(stats_text, reply_markup=back_button)
    except MessageNotModified: pass
    await cb.answer()
