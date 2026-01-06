# handlers/inline_query_handler.py
import logging
import uuid

from pyrogram import Client, filters
from pyrogram.types import (
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from pyrogram.errors import QueryIdInvalid, MessageNotModified # MessageNotModified might not be common here

import config
from db import save_inline_share_content, get_user_setting # get_user_setting for default protections
from utils.decorators import check_user_status # Ensure user is in DB, not banned

LOGGER = logging.getLogger(__name__)

# This is a simplified inline query handler for sharing text secrets.
# It could be expanded to handle different types of inline shares if desired.

@Client.on_inline_query(filters.regex(r"^(?!\s*$).+")) # Match non-empty queries
@check_user_status # User performing inline query should be checked
async def secret_text_inline_handler(client: Client, inline_query: InlineQuery):
    user_id = inline_query.from_user.id
    query_text = inline_query.query.strip() # The text the user wants to share

    if not query_text: # Should be caught by regex, but safeguard
        try: await inline_query.answer([], cache_time=10) # Answer with empty if truly empty
        except QueryIdInvalid: pass # User cleared query too fast
        return

    LOGGER.info(f"User {user_id} inline query for secret text: '{query_text[:50]}...'")

    # --- Store the secret temporarily ---
    # The bot sends the message to itself ("me") to get a persistent message_id.
    # This message can then be copied/forwarded when the recipient views the secret.
    try:
        temp_bot_message = await client.send_message(user_id, text=query_text)
    except Exception as e:
        LOGGER.error(f"Failed to send temporary message to self for inline query from {user_id}: {e}")
        try:
            await inline_query.answer(
                results=[
                    InlineQueryResultArticle(
                        id=str(uuid.uuid4()), # Needs a unique ID
                        title="⚠️ Bot not Started by You. Please Start Bot First.",
                        description=f"Please try again or Start our Bot First. https://t.me/{config.BOT_USERNAME}?start",
                        input_message_content=InputTextMessageContent(f"Please start our Bot First then Try. Start Here: https://t.me/{config.BOT_USERNAME}?start")
                    )
                ],
                cache_time=5 # Short cache for errors
            )
        except QueryIdInvalid: pass # User might have changed query
        except Exception as e_ans: LOGGER.error(f"Error answering inline query with error message: {e_ans}")
        return

    # --- Prepare share document for DB ---
    share_uuid = str(uuid.uuid4())
    access_token = str(uuid.uuid4()) # Unique token for this inline share view link

    # Fetch user's default sharing preferences
    default_show_tag = await get_user_setting(user_id, "default_show_forward_tag")
    default_protect_content = await get_user_setting(user_id, "default_protected_content")

    save_success = await save_inline_share_content(
        sender_id=user_id,
        text_content=query_text, # Storing the raw text
        share_uuid=share_uuid,
        access_token=access_token,
        original_chat_id=temp_bot_message.chat.id, # Chat ID of "me"
        original_message_id=temp_bot_message.id,   # Message ID in "me"
        is_protected=default_protect_content, # Apply user's default
        show_forward_tag=default_show_tag     # Apply user's default
    )

    if not save_success:
        LOGGER.error(f"Failed to save inline share content to DB for user {user_id}, share_uuid {share_uuid}.")
        await temp_bot_message.delete() # Clean up the message sent to "me"
        try:
            await inline_query.answer(
                results=[
                    InlineQueryResultArticle(
                        id=str(uuid.uuid4()),
                        title="⚠️ Database Error",
                        description="Could not save your secret. Please try later.",
                        input_message_content=InputTextMessageContent("Failed to save inline secret due to a database issue.")
                    )
                ],
                cache_time=5
            )
        except QueryIdInvalid: pass
        except Exception as e_ans: LOGGER.error(f"Error answering inline query with DB error message: {e_ans}")
        return

    # --- Construct Inline Query Result ---
    #bot_username = client.me.username
    view_secret_url = f"https://t.me/{config.BOT_USERNAME}?start=viewsecret_{access_token}"

    # The message that will be sent when the user picks this inline result
    input_content_message_text = (
        f"🤫 {inline_query.from_user.mention} has shared a secret text with you!\n\n"
        f"It's a one-time view. Click the link or button below to reveal it.\n\n"
        f"🔗 Link: {view_secret_url}"
    )

    result_article = InlineQueryResultArticle(
        id=share_uuid, # Unique ID for this result (can be share_uuid)
        title="🔒 Share this Text Secretly",
        description=f"'{query_text[:40].replace(chr(10), ' ')}...' (One-time view after sending)", # Preview of text
        input_message_content=InputTextMessageContent(
            message_text=input_content_message_text,
            disable_web_page_preview=True # Good practice for secret links unless preview is intended
        ),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(text="👁️ View Secret (1-Time)", url=view_secret_url)
        ]]),
        thumb_url="https://i.ibb.co/fhySkrD/file-00000000938862309b22c17d0d79588e.png" # Example thumbnail
    )

    try:
        await inline_query.answer(
            results=[result_article],
            cache_time=config.INLINE_QUERY_CACHE_TIME, # From config.py
            is_personal=True, # Results are specific to this user's input
            # switch_pm_text="Configure Defaults?", # Optional: To guide user to bot PM
            # switch_pm_parameter="settings_inline" # Parameter for /start in PM
        )
        LOGGER.info(f"Responded to inline query from {user_id} with share_uuid {share_uuid}.")
        # The temp_bot_message sent to "me" will be deleted when the secret is viewed and "destructed",
        # or if the share expires. For inline shares, this content reference is crucial.
        # No immediate deletion here.
    except QueryIdInvalid:
        LOGGER.warning(f"Query ID became invalid for user {user_id} while answering inline query. Share {share_uuid} created but result not sent.")
        # The share is in DB. If QueryIdInvalid, the user might have cleared text.
        # We should ideally also delete the temp_bot_message and the DB entry if result sending fails.
        # This part needs more robust cleanup if `answer` fails.
        await temp_bot_message.delete()
        from db import delete_share_by_uuid # Import for cleanup
        await delete_share_by_uuid(share_uuid) # Attempt to clean up DB entry
        LOGGER.info(f"Cleaned up share {share_uuid} and temp message due to QueryIdInvalid.")
    except Exception as e:
        LOGGER.error(f"Unexpected error answering inline query for user {user_id}: {e}")
        await temp_bot_message.delete()
        from db import delete_share_by_uuid
        await delete_share_by_uuid(share_uuid)
        LOGGER.info(f"Cleaned up share {share_uuid} and temp message due to unexpected error answering query.")
