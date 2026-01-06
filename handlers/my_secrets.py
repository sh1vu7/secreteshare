import logging
from datetime import datetime, timezone

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery
from pyrogram.errors import MessageNotModified

import config
from db import get_user_shares, get_share_by_uuid, update_share
from utils.keyboards import (
    create_my_secrets_list_keyboard, create_my_secret_detail_keyboard,
    MY_SECRETS_CALLBACK, MY_SECRETS_NAV_PREFIX, MY_SECRETS_DETAIL_PREFIX,
    MY_SECRETS_ACTION_PREFIX, MAIN_MENU_CALLBACK
)
from utils.decorators import check_user_status
from utils.scheduler import cancel_scheduled_job # General job cancellation

LOGGER = logging.getLogger(__name__)

MY_SECRETS_LIST_TEXT = """
🗂️ **My Shared Secrets**

Here are secrets you've shared. Select one for details or actions.
Status: 🟢 Active, 👁️ Viewed, ⏳ Expired, ❌ Revoked, 🔥 Destructed
"""
NO_SECRETS_TEXT = "🗂️ **My Shared Secrets**\n\nYou haven't shared any secrets yet, or all have been cleared."


async def display_my_secrets_list(client: Client, cb: CallbackQuery, user_id: int, page: int = 0):
    LOGGER.info(f"User {user_id} viewing 'My Shared Secrets', page {page}.")
    # Fetch shares that are "active" or "viewed" for management purposes
    # Expired/destructed/revoked are final states and might not need listing here unless desired.
    shares, total_shares_count = await get_user_shares(
        user_id,
        page=page,
        limit=config.MY_SECRETS_PAGE_LIMIT,
        status_filter=["active", "viewed"] # Sender might want to see 'viewed' items too
    )

    text_to_send = MY_SECRETS_LIST_TEXT
    if not shares and page == 0:
        text_to_send = NO_SECRETS_TEXT
    elif not shares and page > 0: # Reached end of pages
        text_to_send += "\nNo more secrets to display on this page."


    keyboard = create_my_secrets_list_keyboard(shares, page, total_shares_count)
    try:
        await cb.edit_message_text(text_to_send, reply_markup=keyboard)
        await cb.answer()
    except MessageNotModified:
        await cb.answer("You are already on this page.")
    except Exception as e:
        LOGGER.error(f"Error displaying 'My Shared Secrets' list for {user_id} (page {page}): {e}")
        await cb.answer("Error loading your shared secrets.", show_alert=True)

@Client.on_callback_query(filters.regex(f"^{MY_SECRETS_CALLBACK}$")) # Matches "main:my_secrets"
@check_user_status
async def my_secrets_entry_handler(client: Client, cb: CallbackQuery):
    await display_my_secrets_list(client, cb, cb.from_user.id, page=0)

@Client.on_callback_query(filters.regex(f"^{MY_SECRETS_NAV_PREFIX}page:"))
@check_user_status
async def my_secrets_nav_handler(client: Client, cb: CallbackQuery):
    user_id = cb.from_user.id
    try:
        page = int(cb.data.split(":")[-1]) # e.g., mysec_nav:page:1 -> 1
    except (IndexError, ValueError):
        LOGGER.error(f"Invalid page number in callback: {cb.data} for user {user_id}")
        await cb.answer("Error: Invalid page.", show_alert=True)
        return
    await display_my_secrets_list(client, cb, user_id, page=page)

@Client.on_callback_query(filters.regex(f"^{MY_SECRETS_DETAIL_PREFIX}"))
@check_user_status
async def my_secret_detail_handler(client: Client, cb: CallbackQuery):
    user_id = cb.from_user.id
    try:
        share_uuid = cb.data.split(MY_SECRETS_DETAIL_PREFIX, 1)[1]
    except IndexError:
        LOGGER.error(f"Invalid share_uuid in detail callback: {cb.data} for user {user_id}")
        await cb.answer("Error: Invalid secret identifier.", show_alert=True)
        return

    share = await get_share_by_uuid(share_uuid, sender_id=user_id) # Ensure ownership

    if not share:
        await cb.answer("Secret not found or you no longer have access.", show_alert=True)
        await display_my_secrets_list(client, cb, user_id, page=0) # Go back to list
        return

    LOGGER.info(f"User {user_id} viewing detail for share_uuid: {share_uuid}")

    text = "📜 **Secret Details**\n\n"
    text += f"**UUID:** `{share['share_uuid']}`\n"
    text += f"**Type:** {'Text' if share['share_type'] == 'message' else 'File/Media'}"
    if share['share_type'].startswith('message_'): text += f" ({share['share_type'].split('_')[1]})" # e.g. (inline)
    text += "\n"

    if share['share_type'] == 'file' and share.get('original_file_name'):
        text += f"**File Name:** `{share['original_file_name']}`\n"

    recipient_name = share.get("recipient_display_name", f"User ID: {share.get('recipient_id')}") if share.get('recipient_id') else None
    if share.get("recipient_type") == "link":
        if not share.get("recipient_id"): # Link not yet claimed
            bot_username = client.me.username
            sharable_link = f"https://t.me/{bot_username}?start=viewsecret_{share['access_token']}"
            recipient_info = f"Sharable Link (Not Claimed)\n  **Link**: `{sharable_link}`"
        else: # Link claimed
            recipient_info = f"Sharable Link (Claimed by {recipient_name or 'Unknown'})"
    else: # Specific user
        recipient_info = recipient_name or "Specific User (Details N/A)"

    text += f"**Shared With:** {recipient_info}\n"
    text += f"**Status:** `{share.get('status', 'N/A').capitalize()}`\n"
    text += f"**Created:** `{share['created_at']:%Y-%m-%d %H:%M} UTC`\n"

    if share.get('expires_at'):
        text += f"**Expires/Destructs At:** `{share['expires_at']:%Y-%m-%d %H:%M} UTC`\n"
    elif share.get('self_destruct_after_view', True) and share.get('status') == 'active':
        text += "**Self-Destructs:** After one view (or timer if set)\n"
    
    if share.get('is_protected_content'):
        text += "**Content Protection:** `Enabled (No Forward/Save)`\n"
    if not share.get('show_forward_tag', True) and share.get('share_type') != 'message_inline': # Inline usually no tag
        text += "**Forward Tag:** `Hidden (Sent as Copy)`\n"


    if share.get('viewed_at'):
        viewed_by_name = share.get('recipient_display_name', share.get('viewed_by_user_id', 'N/A'))
        text += f"**Viewed At:** `{share['viewed_at']:%Y-%m-%d %H:%M} UTC` by `{viewed_by_name}`\n"
    if share.get('revoked_at'):
        text += f"**Revoked At:** `{share['revoked_at']:%Y-%m-%d %H:%M} UTC`\n"
    if share.get('destructed_at'):
        text += f"**Destructed At:** `{share['destructed_at']:%Y-%m-%d %H:%M} UTC`\n"
    if share.get('failure_reason'):
        text += f"**Note:** `Encountered issue: {share['failure_reason']}`\n"

    text += f"**Max Views:** {share.get('max_views_label', str(share.get('max_views', '1')))}\n" # Use stored label or number
    text += f"**Views So Far:** {share.get('view_count', 0)}\n"

    keyboard = create_my_secret_detail_keyboard(share)
    try:
        await cb.edit_message_text(text, reply_markup=keyboard)
        await cb.answer()
    except MessageNotModified:
        await cb.answer() # No alert if not modified is fine
    except Exception as e:
        LOGGER.error(f"Error displaying secret detail {share_uuid} for {user_id}: {e}")
        await cb.answer("Error loading details.", show_alert=True)

@Client.on_callback_query(filters.regex(f"^{MY_SECRETS_ACTION_PREFIX}revoke:"))
@check_user_status
async def my_secret_action_handler(client: Client, cb: CallbackQuery):
    user_id = cb.from_user.id
    try:
        # e.g. mysec_action:revoke:share_uuid
        parts = cb.data.split(":")
        action_type = parts[1]
        share_uuid = parts[2]
    except IndexError:
        LOGGER.error(f"Invalid 'My Secrets' action callback: {cb.data} for user {user_id}")
        await cb.answer("Error: Invalid action.", show_alert=True)
        return

    share = await get_share_by_uuid(share_uuid, sender_id=user_id) # Verify ownership
    if not share:
        await cb.answer("Secret not found or action not permitted.", show_alert=True)
        return

    LOGGER.info(f"User {user_id} performing '{action_type}' on share {share_uuid}")

    if action_type == "revoke":
        if share.get("status") not in ["active", "viewed"]:
            await cb.answer(f"Cannot revoke. Secret is already {share.get('status', 'processed')}.", show_alert=True)
            return

        revoked_time = datetime.now(timezone.utc)
        updates = {
            "status": "revoked",
            "revoked_at": revoked_time,
            "expires_at": revoked_time # Mark as immediately expired upon revoke
        }
        success = await update_share(share_uuid, updates)

        if success:
            job_id_to_cancel = None
            if share.get("bot_message_id_to_recipient") and share.get("recipient_id"):
                # Job for deleting the "View Secret" button message
                job_id_to_cancel = f"del_msg_{share['recipient_id']}_{share['bot_message_id_to_recipient']}_{share['share_uuid']}"
                if cancel_scheduled_job(job_id_to_cancel):
                    LOGGER.info(f"Cancelled job {job_id_to_cancel} for revoked share's control message.")
            elif share.get("recipient_type") == "link" and share.get("access_token"):
                # Job for link expiry
                job_id_to_cancel = f"expire_share_{share['share_uuid']}_{share['access_token']}"
                if cancel_scheduled_job(job_id_to_cancel):
                     LOGGER.info(f"Cancelled job {job_id_to_cancel} for revoked share's link expiry.")

            # Attempt to delete the "View Secret" button message if it was sent to a specific user and still active
            if share.get("status") == "active" and \
               share.get("bot_message_id_to_recipient") and \
               share.get("recipient_id"):
                try:
                    await client.delete_messages(
                        chat_id=share["recipient_id"],
                        message_ids=share["bot_message_id_to_recipient"]
                    )
                    LOGGER.info(f"Deleted 'View Secret' button msg for share {share_uuid} from recipient {share['recipient_id']}.")
                except Exception as e_del:
                    LOGGER.warning(f"Could not delete 'View Secret' button for {share_uuid}: {e_del}. May already be gone.")

            await cb.answer("Secret revoked successfully!", show_alert=False)
            # Refresh the detail view to show 'revoked' status
            cb.data = f"{MY_SECRETS_DETAIL_PREFIX}{share_uuid}" # Trigger detail view again
            await my_secret_detail_handler(client, cb) # The detail handler will answer the callback again
            return
        else:
            await cb.answer("Error: Failed to revoke the secret.", show_alert=True)
    else:
        await cb.answer(f"Unknown action: {action_type}", show_alert=True)