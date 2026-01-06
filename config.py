import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/secret_share_bot_default_db")

OWNER_ID = int(os.getenv("OWNER_ID", "0"))

FREE_TIER_MAX_FILE_SIZE_MB = int(os.getenv("FREE_TIER_MAX_FILE_SIZE_MB", 1024))
FREE_TIER_DEFAULT_EXPIRY_HOURS = int(os.getenv("FREE_TIER_DEFAULT_EXPIRY_HOURS", 87600))
FREE_SELF_DESTRUCT_OPTIONS = [1, 5, 10, 30, 60, 120, 360, 720, 1440] # Mins: 1m, 5m, 10m, 30m, 1h, 2h, 6h, 12h, 1d
FREE_TIER_MAX_EXPIRY_DAYS = int(os.getenv("FREE_TIER_MAX_EXPIRY_DAYS", 784759689777))
PREMIUM_TIER_MAX_FILE_SIZE_MB = int(os.getenv("PREMIUM_TIER_MAX_FILE_SIZE_MB", 2048))
PREMIUM_TIER_MAX_EXPIRY_DAYS = int(os.getenv("PREMIUM_TIER_MAX_EXPIRY_DAYS", 10000000000000))
PREMIUM_SELF_DESTRUCT_OPTIONS = [1, 5, 10, 30, 60, 120, 360, 720, 1440, 2880] # Mins: 1m, 5m, 10m, 30m, 1h, 2h, 6h, 12h, 1d, 2d

BOT_USERNAME = os.getenv("BOT_USERNAME", "YourSecretShareBot") # Will be updated by app.get_me() in main.py
MAX_MESSAGE_LENGTH_FOR_SECRET = int(os.getenv("MAX_MESSAGE_LENGTH_FOR_SECRET", 4000))
TEMP_DOWNLOAD_DIR = os.path.join(os.getcwd(), "temp_downloads")
os.makedirs(TEMP_DOWNLOAD_DIR, exist_ok=True)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

DEFAULT_USER_SETTINGS = {
    "notify_on_view": True,
    "default_protected_content": False, # Default choice for new shares (user can override)
    "default_show_forward_tag": True, # Default choice for new shares (user can override)
}

MY_SECRETS_PAGE_LIMIT = 5
MAX_CONCURRENT_SHARES_FREE = int(os.getenv("MAX_CONCURRENT_SHARES_FREE", 500))
MAX_CONCURRENT_SHARES_PREMIUM = int(os.getenv("MAX_CONCURRENT_SHARES_PREMIUM", 4000))

INLINE_QUERY_CACHE_TIME = int(os.getenv("INLINE_QUERY_CACHE_TIME", 300)) # Cache time for inline query results

SUDO_USERS = [int(user_id.strip()) for user_id in os.getenv("SUDO_USERS", "").split(',') if user_id.strip().isdigit()]
SUDO_USERS.append(OWNER_ID) # Owner is always a sudo user
SUDO_USERS = sorted(list(set(SUDO_USERS))) # Ensure uniqueness and sorted, with owner included

# In config.py
FREE_TIER_DEFAULT_MAX_VIEWS = 1000000 # Could be lower by default
FREE_TIER_MAX_ALLOWED_MAX_VIEWS = 5000000 # e.g., free users can set up to 5 views
FREE_MAX_VIEWS_OPTIONS = [1, 2, 30, 500, 10000, 250000, 5000000] # 0 for unlimited (or very high number internally)

PREMIUM_TIER_DEFAULT_MAX_VIEWS = 3000000 # Could be higher by default
PREMIUM_TIER_MAX_ALLOWED_MAX_VIEWS = 10000000 # e.g., premium can set many more
PREMIUM_MAX_VIEWS_OPTIONS = [1, 2, 30, 500, 10000, 250000, 5000000, 0] # 0 for unlimited (or very high number internally)

def validate_config():
    critical_vars = {
        "API_ID": API_ID,
        "API_HASH": API_HASH,
        "BOT_TOKEN": BOT_TOKEN,
        "MONGO_URI": MONGO_URI,
        "OWNER_ID": OWNER_ID,
    }
    missing_vars = []
    for key, value in critical_vars.items():
        if isinstance(value, int) and value == 0:
            missing_vars.append(f"{key} (is 0)")
        elif isinstance(value, str) and not value:
            missing_vars.append(f"{key} (is empty)")

    if missing_vars:
        raise ValueError(
            f"Missing or placeholder configuration for: {', '.join(missing_vars)}. "
            "Please update config.py or .env file."
        )

    if MONGO_URI == "mongodb://localhost:27017/secret_share_bot_default_db":
        print(
            "INFO: Using default MongoDB URI. Ensure MongoDB is running and accessible, or update MONGO_URI."
        )

if __name__ == "__main__":
    print("Configuration Loaded (Sample - some values may be sensitive):")
    print(f"  API_ID: {'*' * len(str(API_ID)) if API_ID else 'Not Set'}")
    print(f"  API_HASH: {'*' * len(API_HASH) if API_HASH else 'Not Set'}")
    print(f"  BOT_TOKEN: {'*' * (len(BOT_TOKEN)-5) + BOT_TOKEN[-5:] if BOT_TOKEN and len(BOT_TOKEN) > 5 else 'Not Set/Too Short'}")
    print(f"  MONGO_URI: {MONGO_URI}")
    print(f"  OWNER_ID: {OWNER_ID if OWNER_ID else 'Not Set'}")
    print(f"  SUDO_USERS: {SUDO_USERS}")
    print(f"  DEFAULT_USER_SETTINGS: {DEFAULT_USER_SETTINGS}")
    print(f"  PREMIUM_SELF_DESTRUCT_OPTIONS: {PREMIUM_SELF_DESTRUCT_OPTIONS}")
    print(f"  MAX_CONCURRENT_SHARES_FREE: {MAX_CONCURRENT_SHARES_FREE}")
    print(f"  MAX_CONCURRENT_SHARES_PREMIUM: {MAX_CONCURRENT_SHARES_PREMIUM}")
    print(f"  INLINE_QUERY_CACHE_TIME: {INLINE_QUERY_CACHE_TIME}")


    try:
        validate_config()
        print("\nConfig validation passed (basic check).")
    except ValueError as e:
        print(f"\nConfig validation FAILED: {e}")