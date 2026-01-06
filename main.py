import logging
import os
import asyncio # Added for asyncio.run() explicit management in __main__

from pyrogram import Client, idle
from pyrogram.errors import ApiIdInvalid, AuthKeyUnregistered, BotMethodInvalid, RPCError

import dns.resolver
dns.resolver.default_resolver=dns.resolver.Resolver(configure=False)
dns.resolver.default_resolver.nameservers=['8.8.8.8']

import config
from db import init_db, close_db, database as db_instance, pymongo_client as sync_mongo_client # Renamed imported db object
from utils.scheduler import init_scheduler, stop_scheduler, get_scheduler # get_scheduler can be useful
from utils.user_states import clear_user_state # Good to have available for cleanup if needed

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(name)s [%(levelname)s] - %(message)s", # Slightly tweaked format
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(),
        # Consider adding a FileHandler for persistent logs in production
        # logging.FileHandler("bot.log", mode='a', encoding='utf-8'),
    ],
)
LOGGER = logging.getLogger(__name__)
# Reduce verbosity of some third-party loggers
logging.getLogger("pyrogram.session.session").setLevel(logging.WARNING)
logging.getLogger("pyrogram.client").setLevel(logging.WARNING)
logging.getLogger("apscheduler.scheduler").setLevel(logging.INFO) # APScheduler can be verbose on DEBUG

os.makedirs(config.TEMP_DOWNLOAD_DIR, exist_ok=True)

async def main_bot_logic():
    LOGGER.info(f"SecretShareBot is firing up! Version: {getattr(config, 'BOT_VERSION', 'N/A')}") # Add BOT_VERSION to config if desired

    try:
        config.validate_config()
        LOGGER.info("Configuration parameters validated successfully.")
    except ValueError as e:
        LOGGER.critical(f"CRITICAL CONFIGURATION ERROR: {e}. Bot cannot start.")
        return

    try:
        await init_db() # Initializes db_instance and sync_mongo_client from db.py
        LOGGER.info("Database connection established and collections/indexes ensured.")
    except Exception as e:
        LOGGER.critical(f"FATAL: Failed to connect to MongoDB or initialize DB: {e}")
        LOGGER.critical("Ensure MongoDB is running and MONGO_URI is correct in config.")
        return

    scheduler_instance = None
    try:
        # Pass the synchronous pymongo client (obtained from db.init_db) to the scheduler
        scheduler_instance = init_scheduler(pymongo_sync_client=sync_mongo_client)
        if not scheduler_instance or not scheduler_instance.running:
            raise RuntimeError("Scheduler did not start correctly.")
        LOGGER.info("APScheduler initialized and started successfully.")
    except Exception as e:
        LOGGER.critical(f"FATAL: Failed to initialize APScheduler: {e}")
        await close_db() # Close DB if scheduler fails as it's often critical
        return

    # Pyrogram Client Setup
    # Plugins root points to the 'handlers' directory/package.
    app = Client(
        name="SecretShareBotSession", # Session file name
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
        plugins={"root": "handlers"},
        workers=20, # Default, adjust based on load
    )

    # Attach custom attributes to the client instance for easy access in handlers
    setattr(app, 'db', db_instance) # The async Motor database instance
    setattr(app, 'scheduler', scheduler_instance)
    setattr(app, 'owner_id', config.OWNER_ID) # Make owner_id easily accessible from client
    # No need to set app.bot_username or app.bot_id here; get_me() will do it after start.

    LOGGER.info("Attempting to start Pyrogram client...")
    try:
        await app.start()
        bot_info = await app.get_me()
        setattr(app, 'bot_id', bot_info.id)
        setattr(app, 'bot_username', bot_info.username) # Store on client instance
        config.BOT_USERNAME = bot_info.username # Also update config, though client attribute is preferred access
        LOGGER.info(f"Bot @{app.bot_username} (ID: {app.bot_id}) is online and listening!")
        LOGGER.info("Make sure handlers are correctly placed in the 'handlers' directory.")

        await idle() # Keep the bot running until SIGINT, SIGTERM, etc.

    except ApiIdInvalid: LOGGER.critical("API ID or API HASH is invalid. Check config.")
    except AuthKeyUnregistered: LOGGER.critical("Bot token invalid or session corrupted. Delete .session file & re-verify token.")
    except BotMethodInvalid as e: LOGGER.critical(f"Bot API method error: {e}. Possible Pyrogram usage or handler logic issue.")
    except ConnectionError: LOGGER.error("Network error: Could not connect to Telegram.")
    except RPCError as e: LOGGER.error(f"Telegram RPC Error: {e} (Code: {e.ID} - {e.NAME})")
    except KeyboardInterrupt: LOGGER.info("Shutdown signal (KeyboardInterrupt) received.")
    except Exception as e:
        LOGGER.exception(f"An unexpected critical error occurred in main_bot_logic: {e}")
    finally:
        LOGGER.info("Initiating graceful shutdown sequence...")
        if app.is_connected: # Check before trying to stop
            try:
                await app.stop()
                LOGGER.info("Pyrogram client stopped.")
            except Exception as e_stop:
                LOGGER.error(f"Error stopping Pyrogram client: {e_stop}")
        
        current_scheduler = get_scheduler() # Fetch current scheduler instance
        if current_scheduler and current_scheduler.running:
            stop_scheduler() # Uses the stop_scheduler from utils which handles the global instance
            # No need to pass `scheduler_instance` explicitly if stop_scheduler handles the global one
        else:
            LOGGER.info("Scheduler was not running or not initialized for shutdown.")
            
        await close_db() # Closes both motor and pymongo connections
        LOGGER.info("Bot has been shut down. Farewell!")


if __name__ == "__main__":
    # Python 3.7+ standard way to run asyncio programs
    try:
        asyncio.run(main_bot_logic())
    except RuntimeError as e:
        # Suppress "Event loop is closed" error on Windows during forceful exit (Ctrl+C twice sometimes)
        if "Event loop is closed" in str(e) and os.name == 'nt':
            pass
        else:
            LOGGER.critical(f"Runtime error executing main_bot_logic: {e}")
            # raise # Re-raise if it's not the common event loop closed error
    except KeyboardInterrupt:
        # This ensures that if KeyboardInterrupt happens outside the try/finally in main_bot_logic
        # (e.g., during module imports or very early startup), it's caught gracefully.
        LOGGER.info("Main process interrupted by KeyboardInterrupt. Exiting.")
    finally:
        # Perform any final cleanup tasks if needed, though most are in main_bot_logic's finally
        logging.shutdown() # Flushes and closes all logging handlers
