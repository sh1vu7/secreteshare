from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from typing import List, Optional, Dict, Any

import config

MAIN_MENU_CALLBACK = "main:"
SHARE_SECRET_CALLBACK = "main:share"
MY_SECRETS_CALLBACK = "main:my_secrets"
SETTINGS_CALLBACK = "main:settings"
PREMIUM_CALLBACK = "main:premium"
HELP_CALLBACK = "main:help"

SHARE_TYPE_PREFIX = "share_type:"
RECIPIENT_TYPE_PREFIX = "recipient_type:"
PROTECTION_PREF_PREFIX = "share_protect:" # New prefix for protection choices
SET_DESTRUCT_PREFIX = "set_destruct:"
SHARE_CONFIRM_PREFIX = "share_confirm:"
SHARE_CANCEL_PREFIX = "share_cancel:"
VIEW_SECRET_PREFIX = "view_secret:"
FORWARD_TAG_TOGGLE_PREFIX = "fwd_tag:" # Part of protection preferences
PROTECTED_CONTENT_TOGGLE_PREFIX = "prot_cnt:" # Part of protection preferences

SET_MAX_VIEWS_PREFIX = "set_max_views:"
MY_SECRETS_NAV_PREFIX = "mysec_nav:"
MY_SECRETS_DETAIL_PREFIX = "mysec_detail:"
MY_SECRETS_ACTION_PREFIX = "mysec_action:"

SETTINGS_TOGGLE_PREFIX = "settings_toggle:"

ADMIN_PANEL_CALLBACK = "admin:"
ADMIN_USERS_CALLBACK = "admin:users"
ADMIN_BROADCAST_CALLBACK = "admin:broadcast"
ADMIN_STATS_CALLBACK = "admin:stats"
ADMIN_USER_DETAIL_PREFIX = "admin_user:" # Used for back navigation, not direct user action here
ADMIN_PROMOTE_SUDO_PREFIX = "admin_p_sudo:" # Shortened for callback data limit
ADMIN_DEMOTE_SUDO_PREFIX = "admin_d_sudo:"
ADMIN_GRANT_PREMIUM_PREFIX = "admin_g_prem:"
ADMIN_REVOKE_PREMIUM_PREFIX = "admin_r_prem:"
ADMIN_BAN_USER_PREFIX = "admin_ban:"
ADMIN_UNBAN_USER_PREFIX = "admin_unban:"


def create_main_menu_keyboard(is_premium: bool = False, is_sudo: bool = False) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🔒 Share a Secret", callback_data=SHARE_SECRET_CALLBACK)],
        [InlineKeyboardButton("🗂️ My Shared Secrets", callback_data=MY_SECRETS_CALLBACK)],
        [InlineKeyboardButton("⚙️ Settings", callback_data=SETTINGS_CALLBACK)],
    ]
    if not is_premium:
        keyboard.append([InlineKeyboardButton("🌟 Go Premium", callback_data=PREMIUM_CALLBACK)])
    keyboard.append([InlineKeyboardButton("❓ Help & Info", callback_data=HELP_CALLBACK)])
    if is_sudo: # OWNER_ID check will be handled in admin_panel itself, this just shows/hides for sudo.
        keyboard.append([InlineKeyboardButton("👑 Admin Panel", callback_data=ADMIN_PANEL_CALLBACK)])
    return InlineKeyboardMarkup(keyboard)

def create_help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data=f"{MAIN_MENU_CALLBACK}start")]])

def create_share_type_keyboard(share_uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💬 Message", callback_data=f"{SHARE_TYPE_PREFIX}message:{share_uuid}"),
            InlineKeyboardButton("📄 File/Media", callback_data=f"{SHARE_TYPE_PREFIX}file:{share_uuid}")
        ],
        [InlineKeyboardButton("❌ Cancel Share", callback_data=f"{SHARE_CANCEL_PREFIX}now:{share_uuid}")]
    ])

def create_recipient_type_keyboard(share_uuid: str) -> InlineKeyboardMarkup:
    keyboard_rows = [
        [InlineKeyboardButton("👤 Specific User", callback_data=f"{RECIPIENT_TYPE_PREFIX}user:{share_uuid}")],
        [InlineKeyboardButton("🔗 Generate Sharable Link", callback_data=f"{RECIPIENT_TYPE_PREFIX}link:{share_uuid}")]
    ]
    keyboard_rows.append([InlineKeyboardButton("❌ Cancel Share", callback_data=f"{SHARE_CANCEL_PREFIX}now:{share_uuid}")])
    return InlineKeyboardMarkup(keyboard_rows)

# New function
def create_max_views_keyboard(share_uuid: str, is_premium: bool) -> InlineKeyboardMarkup:
    keyboard = []
    
    # Define view options based on user tier
    if is_premium:
        options = config.PREMIUM_MAX_VIEWS_OPTIONS
        # Label for unlimited option for premium (if 0 means unlimited)
        unlimited_label = "No Limit (View-Based Expiry)" if 0 in options else None
    else:
        options = config.FREE_MAX_VIEWS_OPTIONS
        #options = [i for i in range(1, config.FREE_TIER_MAX_ALLOWED_MAX_VIEWS + 1)]
        # Or just a few predefined options for free users:
        # options = [1, 2, 3]
        unlimited_label = None

    row = []
    for views in options:
        if views == 0 and unlimited_label: # Handle '0' for unlimited if present
            label = unlimited_label
        else:
            label = f"{views} View{'s' if views > 1 else ''}"
        
        if len(row) >= 3: # Max 3 buttons per row
            keyboard.append(row)
            row = []
        row.append(InlineKeyboardButton(label, callback_data=f"{SET_MAX_VIEWS_PREFIX}{views}:{share_uuid}"))
    
    if row:
        keyboard.append(row)

    # Default/Skip option (could lead to confirmation or skip if only one choice for tier)
    # For simplicity now, assume selection is mandatory from above.
    # Could add: [InlineKeyboardButton("➡️ Next: Confirm", callback_data=f"{SET_MAX_VIEWS_PREFIX}done:{share_uuid}")]
    # or "Skip (Default 1 View)" if that's desired.
    # For now, make it so selection moves to confirmation.

    keyboard.append([InlineKeyboardButton("❌ Cancel Share", callback_data=f"{SHARE_CANCEL_PREFIX}now:{share_uuid}")])
    return InlineKeyboardMarkup(keyboard)

def create_protection_preferences_keyboard(share_uuid: str, current_show_forward_tag: bool, current_protected_content: bool) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(
                f"Forward Tag: {'✅ Show' if current_show_forward_tag else '☑️ Hide'}",
                callback_data=f"{FORWARD_TAG_TOGGLE_PREFIX}{share_uuid}"
            )
        ],
        [
            InlineKeyboardButton(
                f"Protect Content: {'✅ Yes (No Forward/Save)' if current_protected_content else '☑️ No (Allow Forward/Save)'}",
                callback_data=f"{PROTECTED_CONTENT_TOGGLE_PREFIX}{share_uuid}"
            )
        ],
        [InlineKeyboardButton("➡️ Next: Self-Destruct Options", callback_data=f"{PROTECTION_PREF_PREFIX}done:{share_uuid}")],
        [InlineKeyboardButton("❌ Cancel Share", callback_data=f"{SHARE_CANCEL_PREFIX}now:{share_uuid}")]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_self_destruct_options_keyboard(share_uuid: str, is_premium: bool) -> InlineKeyboardMarkup:
    keyboard: List[List[InlineKeyboardButton]] = []
    options = []
    if is_premium:
        options.extend(config.PREMIUM_SELF_DESTRUCT_OPTIONS)
        default_option_label = "No Timer (Max Lifespan / View-Based)"
        default_option_value = "0" # Represents max lifespan or view-based for premium
    else:
        # # Free users might only have one default option determined by config
        # default_expiry_minutes = config.FREE_TIER_DEFAULT_EXPIRY_HOURS * 60
        # options.append(default_expiry_minutes)
        # default_option_label = f"Default ({config.FREE_TIER_DEFAULT_EXPIRY_HOURS}h)"
        # default_option_value = str(default_expiry_minutes)
        options.extend(config.FREE_SELF_DESTRUCT_OPTIONS)
        default_option_label = "No Timer (Max Lifespan / View-Based)"
        default_option_value = "0" # Represents max lifespan or view-based for premium


    timer_buttons_row = []
    for minutes in options:
        if minutes == 0 and not is_premium: continue # Skip "No Timer" explicit option for free if it maps to default

        if minutes < 60: label = f"{minutes}m"
        elif minutes == 60: label = f"1h"
        elif minutes % 1440 == 0: label = f"{minutes // 1440}d" # Days
        elif minutes % 60 == 0: label = f"{minutes // 60}h" # Hours
        else:
            hours = minutes // 60
            rem_minutes = minutes % 60
            label = f"{hours}h{rem_minutes}m"

        if len(timer_buttons_row) >= 2: # Keep rows somewhat balanced
             keyboard.append(timer_buttons_row)
             timer_buttons_row = []
        timer_buttons_row.append(InlineKeyboardButton(label, callback_data=f"{SET_DESTRUCT_PREFIX}{minutes}:{share_uuid}"))

    if timer_buttons_row:
        keyboard.append(timer_buttons_row)

    if is_premium: # Add the explicit "No Timer" (or view-based/max lifespan) for premium
        keyboard.append([InlineKeyboardButton(default_option_label, callback_data=f"{SET_DESTRUCT_PREFIX}{default_option_value}:{share_uuid}")])
    # elif not options or (options and options[0] != default_expiry_minutes): # If default option for free user not already listed (e.g. if `options` was empty)
    else:
        # This case is unlikely given current logic but safe-guards
        keyboard.append([InlineKeyboardButton(default_option_label, callback_data=f"{SET_DESTRUCT_PREFIX}{default_option_value}:{share_uuid}")])

    keyboard.append([InlineKeyboardButton("❌ Cancel Share", callback_data=f"{SHARE_CANCEL_PREFIX}now:{share_uuid}")])
    return InlineKeyboardMarkup(keyboard)

def create_confirmation_keyboard(share_uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm & Send", callback_data=f"{SHARE_CONFIRM_PREFIX}send:{share_uuid}")],
        [InlineKeyboardButton("❌ Cancel Share", callback_data=f"{SHARE_CANCEL_PREFIX}now:{share_uuid}")]
    ])

def create_view_secret_button(access_token: str, custom_text: Optional[str] = None) -> InlineKeyboardMarkup:
    button_text = custom_text or "🤫 View Secret"
    return InlineKeyboardMarkup([[InlineKeyboardButton(button_text, callback_data=f"{VIEW_SECRET_PREFIX}{access_token}")]])


def create_my_secrets_list_keyboard(shares: List[Dict[str, Any]], current_page: int, total_shares: int) -> InlineKeyboardMarkup:
    keyboard: List[List[InlineKeyboardButton]] = []
    if shares:
        for share in shares:
            recipient_info = share.get("recipient_display_name") or \
                             ("Sharable Link" if share.get("recipient_type") == "link" else "Unknown")
            share_type_emoji = "💬" if share.get("share_type") == "message" else "📄"
            file_name = share.get("original_file_name")
            content_desc = f" ({file_name})" if file_name else ""

            status_map = {"active": "🟢", "viewed": "👁️", "expired": "⏳", "revoked": "❌", "destructed": "🔥"}
            status_emoji = status_map.get(share.get("status", ""), "❓")

            button_text = f"{status_emoji} {share_type_emoji}{content_desc} to {recipient_info}"
            max_len = 40
            if len(button_text.encode('utf-8')) > max_len :
                 button_text = button_text[:max_len//2-3] + "..." + button_text[-max_len//2:]

            keyboard.append([
                InlineKeyboardButton(button_text, callback_data=f"{MY_SECRETS_DETAIL_PREFIX}{share['share_uuid']}")
            ])

    if total_shares > config.MY_SECRETS_PAGE_LIMIT:
        nav_row = []
        if current_page > 0:
            nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{MY_SECRETS_NAV_PREFIX}page:{current_page - 1}"))
        if (current_page + 1) * config.MY_SECRETS_PAGE_LIMIT < total_shares:
            nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"{MY_SECRETS_NAV_PREFIX}page:{current_page + 1}"))
        if nav_row:
            keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("⬅️ Main Menu", callback_data=f"{MAIN_MENU_CALLBACK}start")])
    return InlineKeyboardMarkup(keyboard)

def create_my_secret_detail_keyboard(share: Dict[str, Any]) -> InlineKeyboardMarkup:
    keyboard: List[List[InlineKeyboardButton]] = []
    can_revoke = share.get("status") in ["active", "viewed"]
    if share.get("status") == "viewed" and share.get("recipient_type") != "link":
        # If viewed by specific user & had a control message, usually that message is gone. Revoking may not be meaningful for old button.
        # However, link shares are always revocable until expired or max views (if we implement that)
        # Let's simplify: if 'viewed' but not yet 'expired/destructed', sender can revoke from their side
        pass # can_revoke remains based on above

    if can_revoke:
        keyboard.append([
            InlineKeyboardButton("🚫 Revoke Secret", callback_data=f"{MY_SECRETS_ACTION_PREFIX}revoke:{share['share_uuid']}")
        ])

    keyboard.append([InlineKeyboardButton("⬅️ My Secrets", callback_data=MY_SECRETS_CALLBACK)])
    return InlineKeyboardMarkup(keyboard)

def create_settings_keyboard(user_settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    keyboard: List[List[InlineKeyboardButton]] = []

    notify_on_view_status = "✅ On" if user_settings.get("notify_on_view", False) else "☑️ Off"
    keyboard.append([
        InlineKeyboardButton(
            f"Notify on View: {notify_on_view_status}",
            callback_data=f"{SETTINGS_TOGGLE_PREFIX}notify_on_view"
        )
    ])

    default_prot_cont_status = "✅ Yes" if user_settings.get("default_protected_content", False) else "☑️ No"
    keyboard.append([
        InlineKeyboardButton(
            f"Default Content Protection: {default_prot_cont_status}",
            callback_data=f"{SETTINGS_TOGGLE_PREFIX}default_protected_content"
        )
    ])

    default_fwd_tag_status = "✅ Show" if user_settings.get("default_show_forward_tag", True) else "☑️ Hide"
    keyboard.append([
        InlineKeyboardButton(
            f"Default Forward Tag: {default_fwd_tag_status}",
            callback_data=f"{SETTINGS_TOGGLE_PREFIX}default_show_forward_tag"
        )
    ])

    keyboard.append([InlineKeyboardButton("⬅️ Main Menu", callback_data=f"{MAIN_MENU_CALLBACK}start")])
    return InlineKeyboardMarkup(keyboard)

def create_admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Manage Users", callback_data=ADMIN_USERS_CALLBACK)],
        [InlineKeyboardButton("📢 Broadcast Message", callback_data=ADMIN_BROADCAST_CALLBACK)],
        [InlineKeyboardButton("📊 Bot Stats", callback_data=ADMIN_STATS_CALLBACK)],
        [InlineKeyboardButton("⬅️ Main Menu", callback_data=f"{MAIN_MENU_CALLBACK}start")]
    ])

def create_admin_user_management_keyboard(user_id: int, current_role: str, is_banned: bool, user_is_owner: bool) -> InlineKeyboardMarkup:
    kb = []
    target_is_self = False # Placeholder; in handler, check if admin_user_id == target_user_id

    if not user_is_owner and not target_is_self: # Cannot modify owner or self via this panel
        is_sudo = current_role == "sudo"
        is_premium = current_role == "premium" or (config.DEFAULT_USER_SETTINGS.get("is_premium",False) if current_role == "sudo" else False) # Sudos can be marked premium too


        if not is_sudo:
            kb.append([InlineKeyboardButton("⬆️ Promote Sudo", callback_data=f"{ADMIN_PROMOTE_SUDO_PREFIX}{user_id}")])
        else:
            kb.append([InlineKeyboardButton("⬇️ Demote Sudo", callback_data=f"{ADMIN_DEMOTE_SUDO_PREFIX}{user_id}")])

        # Premium toggle - slightly more complex because sudo is implicitly premium sometimes
        # but we might want an explicit premium flag.
        # current_is_explicitly_premium = user_doc.get("is_premium", False)
        # Logic for this button is tricky based only on `current_role`. Need full user doc ideally.
        # For simplicity: if role is not "premium" or "sudo", offer grant. If "premium" (and not sudo), offer revoke.
        # Sudo users might need separate logic for "explicit premium" if that concept exists.
        # The user document needs an "is_premium" field distinct from "role".
        # Assuming admin_panel.py uses db.get_user() which includes an `is_premium` field.

        #Simplified based on only `current_role` provided - might need to adapt to richer data from calling handler
        if current_role != "premium" and current_role != "sudo": # Regular "free" user
             kb.append([InlineKeyboardButton("🌟 Grant Premium", callback_data=f"{ADMIN_GRANT_PREMIUM_PREFIX}{user_id}")])
        elif current_role == "premium": # Explicitly premium, not sudo
             kb.append([InlineKeyboardButton("⚪ Revoke Premium", callback_data=f"{ADMIN_REVOKE_PREMIUM_PREFIX}{user_id}")])
        # If Sudo, Grant/Revoke premium button might toggle an explicit premium flag
        # For now, if user_doc passed to this keyboard func has `is_explicitly_premium` flag:
        # user_doc = await get_user(user_id)
        # if user_doc.get("is_premium_explicitly_granted", False)
        # For now, simplified to avoid adding extra param just for this button text. Handler has full logic.


        if not is_banned:
            kb.append([InlineKeyboardButton("🚫 Ban User", callback_data=f"{ADMIN_BAN_USER_PREFIX}{user_id}")])
        else:
            kb.append([InlineKeyboardButton("✅ Unban User", callback_data=f"{ADMIN_UNBAN_USER_PREFIX}{user_id}")])

    kb.append([InlineKeyboardButton("⬅️ Admin Panel", callback_data=ADMIN_PANEL_CALLBACK)])
    return InlineKeyboardMarkup(kb)

if __name__ == '__main__':
    print("--- Testing Keyboards ---")
    test_uuid = "test-uuid-123"

    print("\nMain Menu (Free User):")
    print(create_main_menu_keyboard(is_premium=False, is_sudo=False))
    print("\nMain Menu (Premium Sudo):")
    print(create_main_menu_keyboard(is_premium=True, is_sudo=True))

    print("\nShare Type:")
    print(create_share_type_keyboard(test_uuid))

    print("\nRecipient Type:")
    print(create_recipient_type_keyboard(test_uuid))

    print("\nProtection Preferences (Defaults: Tag On, Protect Off):")
    print(create_protection_preferences_keyboard(test_uuid, True, False))
    print("\nProtection Preferences (Tag Off, Protect On):")
    print(create_protection_preferences_keyboard(test_uuid, False, True))


    print("\nSelf Destruct (Free User):")
    print(create_self_destruct_options_keyboard(test_uuid, is_premium=False))
    print("\nSelf Destruct (Premium User):")
    print(create_self_destruct_options_keyboard(test_uuid, is_premium=True))

    print("\nConfirmation:")
    print(create_confirmation_keyboard(test_uuid))

    print("\nView Secret Button:")
    print(create_view_secret_button("test-access-token"))

    print("\nSettings (Default):")
    print(create_settings_keyboard(config.DEFAULT_USER_SETTINGS))
    print("\nSettings (Custom):")
    print(create_settings_keyboard({"notify_on_view": False, "default_protected_content": True, "default_show_forward_tag": False}))

    print("\nAdmin Panel:")
    print(create_admin_panel_keyboard())
    print("\nAdmin User Management (Regular User):")
    print(create_admin_user_management_keyboard(123, "free", False, False))
    print("\nAdmin User Management (Sudo User, Banned):")
    print(create_admin_user_management_keyboard(456, "sudo", True, False))