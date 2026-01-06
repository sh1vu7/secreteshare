import logging
from typing import Any, Optional, List, Dict, Callable # Added Any, List, Dict, Callable

from pyrogram import Client as PyrogramClient
from pyrogram.errors import MessageDeleteForbidden, MessageIdInvalid

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.mongodb import MongoDBJobStore
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.jobstores.base import ConflictingIdError, JobLookupError
from apscheduler.triggers.date import DateTrigger
from datetime import datetime, timezone, timedelta

from pymongo import MongoClient as SyncMongoClient
import config

mongo_client = SyncMongoClient(config.MONGO_URI)
LOGGER = logging.getLogger(__name__)
_scheduler: Optional[AsyncIOScheduler] = None

JOB_ID_PREFIX_DELETE_MESSAGE = "del_msg_"
JOB_ID_PREFIX_EXPIRE_SHARE = "exp_share_"
# JOB_ID_PREFIX_DELETE_INLINE_TEMP = "del_inline_tmp_" # If specific cleanup for inline temp msgs

def _job_listener(event):
    if event.exception:
        LOGGER.error(f"APScheduler job {event.job_id} crashed: {event.exception}\nTraceback: {event.traceback}")
    elif event.code == 8192: # EVENT_JOB_MISSED
        LOGGER.warning(f"APScheduler job {event.job_id} was missed. Check misfire_grace_time and bot uptime.")
    # else: # Successful execution logging can be verbose
    #     LOGGER.debug(f"APScheduler job {event.job_id} executed successfully (Code: {event.code}).")


def init_scheduler(pymongo_sync_client: Optional[SyncMongoClient] = None) -> AsyncIOScheduler:
    global _scheduler
    if _scheduler and _scheduler.running:
        LOGGER.info("APScheduler already initialized and running.")
        return _scheduler

    # Use the global mongo_client if no client is provided.
    if pymongo_sync_client is None:
        pymongo_sync_client = mongo_client

    jobstores = {'default': MemoryJobStore()}  # Default to memory
    job_defaults = {
        'coalesce': True,  # If multiple runs were missed, run once. False means run for each missed.
        'max_instances': 5,
        'misfire_grace_time': 60 * 30  # 30 minutes grace for missed jobs
    }

    if pymongo_sync_client and config.MONGO_URI:
        try:
            pymongo_sync_client.admin.command('ping')
            db_name = config.MONGO_URI.split("/")[-1].split("?")[0]
            if not db_name or db_name == "admin":
                db_name = "SecretShareBot_SchedulerDB"
            jobstores['mongo'] = MongoDBJobStore(
                database=db_name,
                collection='apscheduler_jobs_v3',
                client=pymongo_sync_client
            )
            # Remove default memory store if mongo is used
            jobstores.pop('default', None)
            LOGGER.info(f"APScheduler using MongoDBJobStore (DB: {db_name}, Collection: apscheduler_jobs_v3).")
        except Exception as e:
            LOGGER.error(f"MongoDBJobStore init failed: {e}. APScheduler using MemoryJobStore.")
    else:
        LOGGER.warning("APScheduler: PyMongo client/MONGO_URI invalid. Using MemoryJobStore (jobs won't persist).")

    _scheduler = AsyncIOScheduler(jobstores=jobstores, job_defaults=job_defaults, timezone=timezone.utc)
    from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR, EVENT_JOB_MISSED
    _scheduler.add_listener(_job_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED)

    try:
        _scheduler.start(paused=False)
        LOGGER.info("APScheduler started.")
    except Exception as e:
        LOGGER.error(f"Error starting APScheduler: {e}")
        _scheduler = None
        raise
    return _scheduler


def get_scheduler() -> Optional[AsyncIOScheduler]:
    return _scheduler

def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        try: _scheduler.shutdown(wait=True); LOGGER.info("APScheduler shut down.")
        except Exception as e: LOGGER.error(f"APScheduler shutdown error: {e}")
    elif _scheduler: LOGGER.info("APScheduler was not running.")
    _scheduler = None


async def _execute_message_deletion_job(
    app_client: PyrogramClient,
    chat_id: int,
    message_id: int,
    share_uuid: Optional[str] = None
):
    from db import update_share # Local import
    job_description = f"msg {message_id} in chat {chat_id} (Share: {share_uuid or 'N/A'})"
    LOGGER.info(f"Executing self-destruct for {job_description}")
    try:
        await app_client.delete_messages(chat_id=chat_id, message_ids=message_id)
        LOGGER.info(f"Self-destructed {job_description}.")
        if share_uuid and hasattr(app_client, 'db') and app_client.db:
            success = await update_share(share_uuid, {"status": "destructed", "destructed_at": datetime.now(timezone.utc)})
            if success: LOGGER.info(f"Updated share {share_uuid} status to 'destructed'.")
            else: LOGGER.warning(f"Failed to update share {share_uuid} to 'destructed' after msg deletion.")
    except MessageDeleteForbidden:
        LOGGER.warning(f"Cannot delete {job_description}: Bot lacks permission or message too old.")
        if share_uuid and hasattr(app_client, 'db') and app_client.db:
             await update_share(share_uuid, {"status": "expired", "failure_reason": "delete_forbidden", "expired_at": datetime.now(timezone.utc)})
    except MessageIdInvalid:
        LOGGER.warning(f"Cannot delete {job_description}: Message ID invalid/already deleted.")
    except Exception as e:
        LOGGER.error(f"Error during self-destruct of {job_description}: {e}")


async def _mark_share_as_expired_job(_app_client: PyrogramClient, share_uuid: str):
    from db import shares_collection # Using collection directly for specific query needs of this job
    LOGGER.info(f"Executing expiry for share {share_uuid}")
    try:
        if shares_collection is not None:
            # Only expire if status is 'active'. If it's 'viewed', 'revoked', etc., timer shouldn't override.
            result = await shares_collection.update_one(
                {"share_uuid": share_uuid, "status": "active"},
                {"$set": {"status": "expired", "expired_at": datetime.now(timezone.utc)}}
            )
            if result.modified_count > 0: 
                LOGGER.info(f"Marked share {share_uuid} as 'expired' by timer.")
            else: 
                LOGGER.info(f"Share {share_uuid} not 'active' or not found for timer-based expiry.")
        else:
            LOGGER.error(f"shares_collection not available to mark share {share_uuid} as expired.")
    except Exception as e:
        LOGGER.error(f"Error in _mark_share_as_expired_job for {share_uuid}: {e}")


async def schedule_generic_task(
    app_client: PyrogramClient,
    task_func: Callable, # Use Callable for better type hint
    run_time: datetime,
    job_id: str,
    args: Optional[List[Any]] = None, # List of Any
    kwargs: Optional[Dict[str, Any]] = None # Dict of str to Any
) -> bool:
    if not _scheduler or not _scheduler.running:
        LOGGER.error(f"Scheduler not active. Cannot schedule job '{job_id}'.")
        return False

    effective_args = [app_client] + (args if args else [])
    effective_kwargs = kwargs if kwargs else {}

    try:
        _scheduler.add_job(
            task_func, trigger='date', run_date=run_time,
            args=effective_args, kwargs=effective_kwargs, id=job_id,
            replace_existing=True # Overwrites if job_id exists
        )
        LOGGER.info(f"Scheduled job '{job_id}' for {run_time:%Y-%m-%d %H:%M:%S %Z}")
        return True
    except ConflictingIdError: # Should be rare with replace_existing=True
        LOGGER.warning(f"Job ID '{job_id}' conflict, but replace_existing=True should handle. Investigate if problematic.")
        return True
    except Exception as e:
        LOGGER.error(f"Error scheduling job '{job_id}': {e}")
        return False

def cancel_scheduled_job(job_id: str) -> bool:
    if not _scheduler: LOGGER.warning("Scheduler not active, cannot cancel job."); return False
    try:
        _scheduler.remove_job(job_id)
        LOGGER.info(f"Cancelled scheduled job: '{job_id}'")
        return True
    except JobLookupError:
        LOGGER.warning(f"Job '{job_id}' not found for cancellation (may have run/been removed).")
        return False
    except Exception as e:
        LOGGER.error(f"Error cancelling job '{job_id}': {e}")
        return False

# --- Specific Task Schedulers ---
async def schedule_message_deletion(
    app_client: PyrogramClient, chat_id: int, message_id: int,
    destruction_time: datetime, share_uuid: Optional[str] = None
):
    # Job ID Convention: del_msg_<chat_id>_<message_id>[_share_uuid]
    job_id_suffix = f"_{share_uuid}" if share_uuid else "_timer"
    job_id = f"{JOB_ID_PREFIX_DELETE_MESSAGE}{chat_id}_{message_id}{job_id_suffix}"
    return await schedule_generic_task(
        app_client, _execute_message_deletion_job, destruction_time, job_id,
        args=[chat_id, message_id, share_uuid]
    )

async def schedule_share_expiry(app_client: PyrogramClient, share_uuid: str, expiry_time: datetime):
    # Job ID Convention: exp_share_<share_uuid>
    job_id = f"{JOB_ID_PREFIX_EXPIRE_SHARE}{share_uuid}"
    return await schedule_generic_task(
        app_client, _mark_share_as_expired_job, expiry_time, job_id,
        args=[share_uuid]
    )

async def schedule_inline_temp_message_cleanup(app_client: PyrogramClient, chat_id: int, message_id: int, expiry_time: datetime, share_uuid: str):
    """Schedules cleanup for the temporary message bot sent to itself for an inline share."""
    # This job just deletes the message. Status of the share is handled by view/expiry of share itself.
    # job_id = f"{JOB_ID_PREFIX_DELETE_INLINE_TEMP}{chat_id}_{message_id}_{share_uuid}"
    # Re-using generic message deletion with a clear share_uuid relation might be enough,
    # or give it a slightly different job ID pattern if different logic for this message is ever needed.
    # Let's use existing message deletion with a special share_uuid pattern for identification if needed later
    # e.g. share_uuid could be prefixed like "inline_tmp_for_actual_uuid_XYZ" if it helps disambiguate.
    # For now, this will use the _execute_message_deletion_job, which also updates share status.
    # This might be undesirable if the temp message is just for content reference.
    # Alternative: A simpler _delete_message_only_job for temp messages.
    # For now, using existing one. If share_uuid refers to the actual share, it's fine.
    job_id = f"{JOB_ID_PREFIX_DELETE_MESSAGE}inline_tmp_{chat_id}_{message_id}_{share_uuid}"

    # Using a simplified task that ONLY deletes the message, doesn't touch share status.
    async def _delete_temp_inline_message_task(client, temp_chat_id, temp_msg_id, s_uuid):
        LOGGER.info(f"Cleaning up inline temp message {temp_msg_id} in {temp_chat_id} for share {s_uuid}")
        try: await client.delete_messages(temp_chat_id, temp_msg_id)
        except Exception as e_del_tmp: LOGGER.error(f"Failed to delete inline temp msg {temp_msg_id}: {e_del_tmp}")

    return await schedule_generic_task(
        app_client, _delete_temp_inline_message_task, expiry_time, job_id,
        args=[chat_id, message_id, share_uuid] # Pass share_uuid for logging/ID.
    )

if __name__ == "__main__":
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    logging.basicConfig(level=logging.DEBUG)
    LOGGER.info("Testing APScheduler functions (utils.scheduler)...")

    async def run_scheduler_module_tests():
        mock_pymongo_client = MagicMock(spec=SyncMongoClient)
        mock_pymongo_client.admin.command.return_value = {"ok": 1}
        scheduler = init_scheduler(pymongo_sync_client=mock_pymongo_client)
        assert scheduler is not None and scheduler.running

        mock_app_client = AsyncMock(spec=PyrogramClient)
        mock_app_client.db = MagicMock() # For jobs needing app_client.db

        # Test Message Deletion Schedule
        chat_id_del, msg_id_del, share_uuid_del = -1001, 123, "shareDel1"
        del_time = datetime.now(timezone.utc) + timedelta(seconds=2)
        await schedule_message_deletion(mock_app_client, chat_id_del, msg_id_del, del_time, share_uuid_del)
        expected_del_job_id = f"{JOB_ID_PREFIX_DELETE_MESSAGE}{chat_id_del}_{msg_id_del}_{share_uuid_del}"
        assert scheduler.get_job(expected_del_job_id) is not None

        # Test Share Expiry Schedule
        share_uuid_exp = "shareExp1"
        exp_time = datetime.now(timezone.utc) + timedelta(seconds=3)
        await schedule_share_expiry(mock_app_client, share_uuid_exp, exp_time)
        expected_exp_job_id = f"{JOB_ID_PREFIX_EXPIRE_SHARE}{share_uuid_exp}"
        assert scheduler.get_job(expected_exp_job_id) is not None

        # Test Inline Temp Message Cleanup
        inline_chat_id, inline_msg_id, inline_share_uuid = -1002, 456, "inlineShare1"
        inline_cleanup_time = datetime.now(timezone.utc) + timedelta(seconds=2.5)
        await schedule_inline_temp_message_cleanup(mock_app_client, inline_chat_id, inline_msg_id, inline_cleanup_time, inline_share_uuid)
        expected_inline_cleanup_job_id = f"{JOB_ID_PREFIX_DELETE_MESSAGE}inline_tmp_{inline_chat_id}_{inline_msg_id}_{inline_share_uuid}"
        assert scheduler.get_job(expected_inline_cleanup_job_id) is not None

        # Test Job Cancellation
        job_to_cancel_id = f"{JOB_ID_PREFIX_EXPIRE_SHARE}cancelThisShare"
        await schedule_share_expiry(mock_app_client, "cancelThisShare", datetime.now(timezone.utc) + timedelta(hours=1))
        assert scheduler.get_job(job_to_cancel_id) is not None
        cancel_scheduled_job(job_to_cancel_id)
        assert scheduler.get_job(job_to_cancel_id) is None

        LOGGER.info("Waiting for some jobs to execute...")
        await asyncio.sleep(4) # Allow time for del/exp jobs

        # Check if delete_messages was called (basic check)
        mock_app_client.delete_messages.assert_called()

        stop_scheduler()
        assert _scheduler is None
        LOGGER.info("APScheduler module tests completed.")

    asyncio.run(run_scheduler_module_tests())