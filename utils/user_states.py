import logging
from typing import Dict, Any, Optional, Tuple
from enum import Enum, auto
from uuid import uuid4

LOGGER = logging.getLogger(__name__)

class UserState(Enum):
    DEFAULT = auto()
    AWAITING_SHARE_CONTENT = auto()
    AWAITING_RECIPIENT = auto()
    AWAITING_PROTECTION_PREFERENCES = auto() # New state for forward tag and content protection
    AWAITING_SELF_DESTRUCT_CHOICE = auto()
    AWAITING_MAX_VIEWS_CHOICE = auto()
    AWAITING_CONFIRMATION = auto()
    AWAITING_BROADCAST_MESSAGE = auto()
    AWAITING_BAN_REASON = auto()
    # --- Inline Query Specific States (Optional, can be handled within inline query handler if simple)
    # AWAITING_INLINE_SECRET_CONFIRMATION = auto() # If inline sharing needs a confirmation step via message

_user_states: Dict[int, Tuple[UserState, Dict[str, Any]]] = {}

def get_user_state(user_id: int) -> Tuple[UserState, Dict[str, Any]]:
    return _user_states.get(user_id, (UserState.DEFAULT, {}))

def set_user_state(user_id: int, state: UserState, data: Optional[Dict[str, Any]] = None):
    _user_states[user_id] = (state, data if data is not None else {})
    LOGGER.debug(f"User {user_id} state set to {state.name} with data: {_user_states[user_id][1]}")

def clear_user_state(user_id: int):
    if user_id in _user_states:
        current_state_name = _user_states[user_id][0].name
        del _user_states[user_id]
        LOGGER.debug(f"State {current_state_name} cleared for user {user_id}")
    else:
        LOGGER.debug(f"No state to clear for user {user_id}")

def start_share_flow(user_id: int) -> str:
    share_uuid = str(uuid4())
    set_user_state(user_id, UserState.AWAITING_SHARE_CONTENT, {"share_uuid": share_uuid})
    LOGGER.info(f"User {user_id} started share flow with share_uuid: {share_uuid}")
    return share_uuid

def get_share_flow_data(user_id: int) -> Optional[Dict[str, Any]]:
    state, data = get_user_state(user_id)
    share_flow_states = [
        UserState.AWAITING_SHARE_CONTENT,
        UserState.AWAITING_RECIPIENT,
        UserState.AWAITING_PROTECTION_PREFERENCES,
        UserState.AWAITING_SELF_DESTRUCT_CHOICE,
        UserState.AWAITING_MAX_VIEWS_CHOICE,
        UserState.AWAITING_CONFIRMATION,
    ]
    if state in share_flow_states and "share_uuid" in data:
        return data
    return None

def update_share_flow_data(user_id: int, **kwargs) -> bool:
    state, data = get_user_state(user_id)
    share_flow_states = [
        UserState.AWAITING_SHARE_CONTENT,
        UserState.AWAITING_RECIPIENT,
        UserState.AWAITING_PROTECTION_PREFERENCES,
        UserState.AWAITING_SELF_DESTRUCT_CHOICE,
        UserState.AWAITING_MAX_VIEWS_CHOICE,
        UserState.AWAITING_CONFIRMATION,
    ]
    if state in share_flow_states and "share_uuid" in data:
        data.update(kwargs)
        set_user_state(user_id, state, data)
        return True
    LOGGER.warning(f"Failed to update share flow data for user {user_id}. State: {state.name}, Data: {data}")
    return False

def advance_share_flow_state(user_id: int, new_state: UserState, new_data_to_add: Optional[Dict[str, Any]] = None):
    current_state, current_data = get_user_state(user_id)
    if "share_uuid" not in current_data:
        LOGGER.error(f"Cannot advance share flow for {user_id}: share_uuid missing from current data {current_data} for state {current_state.name}.")
        clear_user_state(user_id)
        return

    if new_data_to_add:
        current_data.update(new_data_to_add)

    set_user_state(user_id, new_state, current_data)

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    LOGGER.info("Running UserState tests...")

    test_user_id = 1001
    test_user_id_2 = 1002

    state, data = get_user_state(test_user_id)
    print(f"User {test_user_id} initial state: {state.name}, data: {data}")
    assert state == UserState.DEFAULT and not data

    share_uuid = start_share_flow(test_user_id)
    state, data = get_user_state(test_user_id)
    print(f"User {test_user_id} after starting share: {state.name}, UUID: {data.get('share_uuid')}")
    assert state == UserState.AWAITING_SHARE_CONTENT and data.get("share_uuid") == share_uuid

    update_success = update_share_flow_data(test_user_id, content_type="text", content_id="msg123")
    state, data = get_user_state(test_user_id)
    print(f"User {test_user_id} after updating data (success: {update_success}): {state.name}, data: {data}")
    assert update_success and data.get("content_type") == "text"

    advance_share_flow_state(test_user_id, UserState.AWAITING_RECIPIENT, {"recipient_type_chosen": "user"})
    state, data = get_user_state(test_user_id)
    print(f"User {test_user_id} after advancing to AWAITING_RECIPIENT: {state.name}, data: {data}")
    assert state == UserState.AWAITING_RECIPIENT and data.get("recipient_type_chosen") == "user"

    advance_share_flow_state(test_user_id, UserState.AWAITING_PROTECTION_PREFERENCES, {"some_pref": True})
    state, data = get_user_state(test_user_id)
    print(f"User {test_user_id} after advancing to AWAITING_PROTECTION_PREFERENCES: {state.name}, data: {data}")
    assert state == UserState.AWAITING_PROTECTION_PREFERENCES and data.get("some_pref") is True

    flow_data = get_share_flow_data(test_user_id)
    print(f"Retrieved share flow data: {flow_data}")
    assert flow_data is not None and flow_data.get("share_uuid") == share_uuid

    state_other, _ = get_user_state(test_user_id_2)
    print(f"User {test_user_id_2} state (should be default): {state_other.name}")
    assert state_other == UserState.DEFAULT

    clear_user_state(test_user_id)
    state, data = get_user_state(test_user_id)
    print(f"User {test_user_id} after clearing state: {state.name}, data: {data}")
    assert state == UserState.DEFAULT and not data

    flow_data_after_clear = get_share_flow_data(test_user_id)
    print(f"Share flow data after clear (should be None): {flow_data_after_clear}")
    assert flow_data_after_clear is None

    set_user_state(test_user_id, UserState.AWAITING_BROADCAST_MESSAGE)
    state, data = get_user_state(test_user_id)
    print(f"User {test_user_id} set to AWAITING_BROADCAST_MESSAGE: {state.name}, data: {data}")
    assert state == UserState.AWAITING_BROADCAST_MESSAGE and not data

    LOGGER.info("UserState tests completed.")

    print("\nAll UserState Enums:")
    for s_enum in UserState:
        print(f"- {s_enum.name} ({s_enum.value})")