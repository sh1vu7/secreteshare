import logging
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone, timedelta

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase, AsyncIOMotorCollection
from pymongo import MongoClient, TEXT, DESCENDING, ASCENDING
from pymongo.errors import OperationFailure

import dns.resolver
dns.resolver.default_resolver=dns.resolver.Resolver(configure=False)
dns.resolver.default_resolver.nameservers=['8.8.8.8']

import config

LOGGER = logging.getLogger(__name__)

motor_client: Optional[AsyncIOMotorClient] = None
database: Optional[AsyncIOMotorDatabase] = None
pymongo_client: Optional[MongoClient] = None

USERS_COLLECTION_NAME = "users"
SHARES_COLLECTION_NAME = "shares"
ADMIN_SETTINGS_COLLECTION_NAME = "admin_settings" # For potential bot-wide settings by admin

users_collection: Optional[AsyncIOMotorCollection] = None
shares_collection: Optional[AsyncIOMotorCollection] = None
admin_settings_collection: Optional[AsyncIOMotorCollection] = None

async def init_db():
    global motor_client, database, pymongo_client
    global users_collection, shares_collection, admin_settings_collection

    LOGGER.info(f"Connecting to MongoDB: {config.MONGO_URI}")
    try:
        motor_client = AsyncIOMotorClient(config.MONGO_URI)
        await motor_client.admin.command("ping")
        db_name_from_uri = config.MONGO_URI.split("/")[-1].split("?")[0]
        if not db_name_from_uri or db_name_from_uri == "admin": # Default if no db name in URI
             db_name_from_uri = "SecretShareBotDB" # Fallback DB name
             LOGGER.warning(f"No specific database name in MONGO_URI, using default: {db_name_from_uri}")
        database = motor_client[db_name_from_uri]
        LOGGER.info(f"Async MongoDB connection successful to database: '{db_name_from_uri}'")
    except Exception as e:
        LOGGER.error(f"Async MongoDB connection failed: {e}")
        raise

    try:
        pymongo_client = MongoClient(config.MONGO_URI)
        pymongo_client.admin.command("ping")
        LOGGER.info("Sync PyMongo connection successful for APScheduler JobStore.")
    except Exception as e:
        LOGGER.warning(f"Sync PyMongo connection failed: {e}. APScheduler may use MemoryJobStore.")
        pymongo_client = None # Ensure it's None if connection failed

    users_collection = database[USERS_COLLECTION_NAME]
    shares_collection = database[SHARES_COLLECTION_NAME]
    admin_settings_collection = database[ADMIN_SETTINGS_COLLECTION_NAME]

    await _ensure_indexes()
    LOGGER.info("Database initialization complete.")

async def _ensure_indexes():
    # User Collection Indexes
    try:
        await users_collection.create_index("user_id", unique=True)
        await users_collection.create_index("role")
        await users_collection.create_index("banned")
        await users_collection.create_index("is_premium")
        # Add index for settings if specific settings are queried frequently across users
        # await users_collection.create_index("settings.notify_on_view")
        LOGGER.info(f"Indexes ensured for '{USERS_COLLECTION_NAME}'.")
    except OperationFailure as e:
        LOGGER.error(f"Error creating indexes for '{USERS_COLLECTION_NAME}': {e}")

    # Shares Collection Indexes
    try:
        await shares_collection.create_index("share_uuid", unique=True)
        await shares_collection.create_index("access_token", unique=True, sparse=True)
        await shares_collection.create_index([("sender_id", DESCENDING), ("created_at", DESCENDING)])
        await shares_collection.create_index("recipient_id", sparse=True)
        await shares_collection.create_index("status")
        await shares_collection.create_index("expires_at", sparse=True) # Sparse if not all shares have expiry
        # For inline query content matching if storing text directly for search (example)
        # await shares_collection.create_index([("inline_search_text", TEXT)], default_language='english', sparse=True)
        LOGGER.info(f"Indexes ensured for '{SHARES_COLLECTION_NAME}'.")
    except OperationFailure as e:
        LOGGER.error(f"Error creating indexes for '{SHARES_COLLECTION_NAME}': {e}")

    # Admin Settings Collection Indexes
    try:
        await admin_settings_collection.create_index("setting_key", unique=True)
        LOGGER.info(f"Indexes ensured for '{ADMIN_SETTINGS_COLLECTION_NAME}'.")
    except OperationFailure as e:
        LOGGER.error(f"Error creating indexes for '{ADMIN_SETTINGS_COLLECTION_NAME}': {e}")

async def close_db():
    global motor_client, pymongo_client
    if motor_client:
        motor_client.close()
        LOGGER.info("Async MongoDB connection closed.")
    if pymongo_client:
        pymongo_client.close()
        LOGGER.info("Sync PyMongo connection closed.")

async def add_user(user_id: int, first_name: Optional[str] = "User", username: Optional[str] = None) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    user_doc = {
        "user_id": user_id,
        #"first_name": first_name,
        #"username": username,
        "role": "free",
        "is_premium": False, # Explicit premium status
        "premium_expiry": None,
        "is_sudo": user_id in config.SUDO_USERS,
        "banned": False,
        "ban_reason": None,
        "first_seen": now,
        #"last_active": now,
        "settings": config.DEFAULT_USER_SETTINGS.copy(),
        "shares_count": 0, # Keep track of total shares made by user
    }
    if user_doc["is_sudo"]: # Sudos get premium by default (can be configurable)
        user_doc["role"] = "sudo" # Sudo is a higher role than premium
        user_doc["is_premium"] = True

    update_result = await users_collection.update_one(
        {"user_id": user_id},
        {
            "$set": {"last_active": now, "first_name": first_name, "username": username},
            "$setOnInsert": user_doc
        },
        upsert=True
    )
    if update_result.upserted_id:
        LOGGER.info(f"New user added: {user_id} ('{first_name}'), Role: {user_doc['role']}.")
        return user_doc
    else: # User existed
        LOGGER.debug(f"User {user_id} ('{first_name}') last_active updated.")
        # Ensure settings field exists and merge defaults
        existing_user = await users_collection.find_one({"user_id": user_id})
        if existing_user and "settings" not in existing_user:
            await users_collection.update_one(
                {"user_id": user_id},
                {"$set": {"settings": config.DEFAULT_USER_SETTINGS.copy()}}
            )
        elif existing_user and "settings" in existing_user:
            # Merge missing default settings into existing user's settings
            new_settings = existing_user["settings"].copy()
            changed = False
            for key, default_value in config.DEFAULT_USER_SETTINGS.items():
                if key not in new_settings:
                    new_settings[key] = default_value
                    changed = True
            if changed:
                await users_collection.update_one(
                    {"user_id": user_id}, {"$set": {"settings": new_settings}}
                )
        return await get_user(user_id) # Re-fetch to get current complete doc


async def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    if users_collection is None:
        LOGGER.error("users_collection is not initialized.")
        return None
    user_data = await users_collection.find_one({"user_id": user_id})

    if user_data:
        # Ensure default settings are present if some are missing
        current_settings = user_data.get("settings", {})
        merged_settings = config.DEFAULT_USER_SETTINGS.copy()
        merged_settings.update(current_settings) # Override defaults with user's actual settings

        if merged_settings != current_settings: # If settings were actually merged/updated
            await users_collection.update_one({"user_id": user_id}, {"$set": {"settings": merged_settings}})
            user_data["settings"] = merged_settings # Update in-memory dict too

        # Removed automatic sudo sync from config to allow manual updates via update_user_details.


        # Check premium expiry
        if user_data.get("is_premium") and user_data.get("premium_expiry"):
            premium_expiry = user_data["premium_expiry"]
            if premium_expiry.tzinfo is None:
                premium_expiry = premium_expiry.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > premium_expiry:
                LOGGER.info(f"Premium expired for user {user_id}. Reverting to free.")
                updates = {"is_premium": False, "premium_expiry": None}
                if user_data["role"] == "premium": updates["role"] = "free" # Only if role was 'premium'
                await users_collection.update_one({"user_id": user_id}, {"$set": updates})
                user_data.update(updates)
    return user_data


async def update_user_details(user_id: int, updates: Dict[str, Any]) -> bool:
    if "role" in updates: # Special handling for role to sync is_premium/is_sudo
        new_role = updates["role"]
        updates["is_premium"] = new_role in ["premium", "sudo", "owner"]
        updates["is_sudo"] = new_role in ["sudo", "owner"]
        if new_role == "owner":
            updates["is_premium"] = True
            updates["is_sudo"] = True
        elif new_role not in ["premium", "sudo", "owner"]: # e.g. 'free'
            updates["is_premium"] = False # User might still have explicit premium if updates["is_premium"] is passed
                                      # separately after this role update.
                                      # This means a direct role update to 'free' also removes implicit premium from role.
    result = await users_collection.update_one({"user_id": user_id}, {"$set": updates})
    return result.modified_count > 0


async def get_all_user_ids(include_banned: bool = True, role_filter: Optional[str] = None) -> list[int]:
    query = {}
    if not include_banned:
        query["banned"] = {"$ne": True}
    if role_filter:
        query["role"] = role_filter
    users_cursor = users_collection.find(query, {"user_id": 1})
    return [user["user_id"] async for user in users_cursor]

async def get_user_setting(user_id: int, setting_key: str) -> Optional[Any]:
    user_data = await get_user(user_id) # Ensures defaults are handled
    return user_data["settings"].get(setting_key) if user_data and "settings" in user_data else config.DEFAULT_USER_SETTINGS.get(setting_key)


async def update_user_setting(user_id: int, setting_key: str, setting_value: Any) -> bool:
    if setting_key not in config.DEFAULT_USER_SETTINGS:
        LOGGER.warning(f"Attempt to update non-default setting '{setting_key}' for user {user_id}.")
        return False

    result = await users_collection.update_one(
        {"user_id": user_id},
        {"$set": {f"settings.{setting_key}": setting_value}}
    )
    if result.matched_count == 0:
        await add_user(user_id)
        result = await users_collection.update_one(
            {"user_id": user_id}, {"$set": {f"settings.{setting_key}": setting_value}}
        )
    return result.modified_count > 0


async def increment_user_shares_count(user_id: int, amount: int = 1):
    await users_collection.update_one({"user_id": user_id}, {"$inc": {"shares_count": amount}})

# --- Share related DB functions ---
async def create_share(share_doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        result = await shares_collection.insert_one(share_doc)
        await increment_user_shares_count(share_doc["sender_id"])
        return await get_share_by_uuid(share_doc["share_uuid"]) # Return the inserted doc with _id
    except Exception as e:
        LOGGER.error(f"Failed to create share in DB for UUID {share_doc.get('share_uuid')}: {e}")
        return None

async def get_share_by_uuid(share_uuid: str, sender_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    query = {"share_uuid": share_uuid}
    if sender_id: query["sender_id"] = sender_id
    return await shares_collection.find_one(query)

async def get_share_by_access_token(access_token: str) -> Optional[Dict[str, Any]]:
    return await shares_collection.find_one({"access_token": access_token, "status": "active"})

async def update_share(share_uuid: str, updates: Dict[str, Any]) -> bool:
    result = await shares_collection.update_one({"share_uuid": share_uuid}, {"$set": updates})
    return result.modified_count > 0

async def get_user_shares(user_id: int, page: int = 0, limit: int = config.MY_SECRETS_PAGE_LIMIT,
                           status_filter: Optional[List[str]] = None) -> Tuple[List[Dict[str, Any]], int]:
    query = {"sender_id": user_id}
    if status_filter:
        query["status"] = {"$in": status_filter}
    else: # Default: show active and viewed for "My Secrets"
        query["status"] = {"$in": ["active", "viewed"]}

    total_count = await shares_collection.count_documents(query)
    shares_cursor = shares_collection.find(query).sort("created_at", DESCENDING).skip(page * limit).limit(limit)
    shares_list = await shares_cursor.to_list(length=limit)
    return shares_list, total_count

async def count_user_active_shares(user_id: int) -> int:
    return await shares_collection.count_documents({"sender_id": user_id, "status": "active"})

async def delete_share_by_uuid(share_uuid: str) -> bool:
    """ For hard deletion, typically not used directly; status change is preferred. """
    result = await shares_collection.delete_one({"share_uuid": share_uuid})
    return result.deleted_count > 0

# For inline query feature (simplified for now)
async def save_inline_share_content(sender_id: int, text_content: str, share_uuid: str,
                                     access_token: str, original_chat_id: int, original_message_id: int,
                                     is_protected:bool, show_forward_tag:bool) -> bool:
    now = datetime.now(timezone.utc)
    share_doc = {
        "share_uuid": share_uuid,
        "access_token": access_token,
        "sender_id": sender_id,
        "share_type": "message_inline", # Special type for inline shares
        "content_text": text_content, # Store text directly for inline shares
        "original_chat_id": original_chat_id, # Bot's chat with itself (where the message is copied from)
        "original_message_id": original_message_id, # ID of the message in bot's chat with itself
        "is_protected_content": is_protected,
        "show_forward_tag": show_forward_tag, # For inline, this often means use copy_message on retrieval.
        "status": "active", # Inline shares are active immediately
        "recipient_type": "link", # Inline shares are always link based initially
        "created_at": now,
        "expires_at": now + timedelta(hours=config.FREE_TIER_DEFAULT_EXPIRY_HOURS), # Default expiry for inline
        "self_destruct_after_view": True,
        "self_destruct_minutes_set": config.FREE_TIER_DEFAULT_EXPIRY_HOURS * 60,
        "view_count": 0,
        "max_views": 1, # Inline typically 1 view
    }
    result = await shares_collection.insert_one(share_doc)
    if result.inserted_id:
        await increment_user_shares_count(sender_id)
        return True
    return False

async def get_inline_share_content(access_token: str) -> Optional[Dict[str, Any]]:
    share = await shares_collection.find_one({"access_token": access_token, "share_type": "message_inline", "status":"active"})
    return share


if __name__ == '__main__':
    import asyncio

    async def test_db_operations():
        logging.basicConfig(level=logging.DEBUG)
        try:
            await init_db()
            LOGGER.info("DB Initialized for testing.")

            # Test add/get user
            test_user_id = 987654321
            u = await get_user(test_user_id)
            if not u:
                LOGGER.info("User not found, adding...")
                u = await add_user(test_user_id, "Test User", "testuser")
                assert u is not None
                assert u['user_id'] == test_user_id
                assert u['settings']['notify_on_view'] == config.DEFAULT_USER_SETTINGS['notify_on_view']
            LOGGER.info(f"User Data: {u}")

            # Test settings
            await update_user_setting(test_user_id, "notify_on_view", False)
            setting_val = await get_user_setting(test_user_id, "notify_on_view")
            assert setting_val is False
            LOGGER.info(f"User setting 'notify_on_view' updated and retrieved: {setting_val}")
            await update_user_setting(test_user_id, "notify_on_view", True) # Reset

            # Test update_user_details
            await update_user_details(test_user_id, {"role": "premium", "is_premium": True, "premium_expiry": datetime.now(timezone.utc) + timedelta(days=30)})
            u = await get_user(test_user_id)
            assert u['role'] == "premium" and u['is_premium'] is True
            LOGGER.info(f"User updated to premium: {u}")

            # Test share creation
            share_uuid_test = "test-share-" + str(datetime.now().timestamp())
            share_doc_test = {
                "share_uuid": share_uuid_test, "access_token": "test-token", "sender_id": test_user_id,
                "share_type": "message", "original_message_id": 123, "original_chat_id": test_user_id,
                "status": "active", "created_at": datetime.now(timezone.utc),
                "show_forward_tag": True, "is_protected_content": False
            }
            created_share = await create_share(share_doc_test)
            assert created_share and created_share['share_uuid'] == share_uuid_test
            LOGGER.info(f"Share created: {created_share}")

            fetched_share = await get_share_by_uuid(share_uuid_test, sender_id=test_user_id)
            assert fetched_share and fetched_share['share_uuid'] == share_uuid_test
            LOGGER.info(f"Share fetched by UUID: {fetched_share}")

            await update_share(share_uuid_test, {"status": "viewed", "viewed_at": datetime.now(timezone.utc)})
            updated_share = await get_share_by_uuid(share_uuid_test)
            assert updated_share and updated_share['status'] == "viewed"
            LOGGER.info(f"Share status updated to viewed: {updated_share}")

            user_shares, total = await get_user_shares(test_user_id, status_filter=["active", "viewed"])
            LOGGER.info(f"User shares (total {total}): {user_shares}")
            assert any(s['share_uuid'] == share_uuid_test for s in user_shares)

            u_after_share = await get_user(test_user_id)
            LOGGER.info(f"User shares count: {u_after_share.get('shares_count')}")
            assert u_after_share.get('shares_count', 0) > 0


            # Test for SUDO_USERS from config
            if config.SUDO_USERS and test_user_id not in config.SUDO_USERS :
                 test_sudo_id = config.SUDO_USERS[0] # Test with first sudo user from config if any
                 sudo_user = await add_user(test_sudo_id, "Sudo Test", "sudotest")
                 retrieved_sudo_user = await get_user(test_sudo_id)
                 LOGGER.info(f"Sudo user from config: {retrieved_sudo_user}")
                 assert retrieved_sudo_user['is_sudo'] is True
                 assert retrieved_sudo_user['role'] == 'sudo'
                 assert retrieved_sudo_user['is_premium'] is True # Sudos are premium


        except Exception as e:
            LOGGER.exception(f"An error occurred during DB testing: {e}")
        finally:
            await close_db()
            LOGGER.info("DB connection closed after testing.")

    asyncio.run(test_db_operations())
