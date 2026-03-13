import os
from celery import Celery

# Celery app uses SQS as broker.
# For local development WITHOUT SQS, set CELERY_TASK_ALWAYS_EAGER=true in .env —
# tasks run synchronously in the same process (no workers or queues needed).
#
# When CELERY_TASK_ALWAYS_EAGER=true, tasks run inside the FastAPI event loop.
# Since workers use asyncio.run(), we apply nest_asyncio to allow nested loops.
# Replace with a proper Celery worker process in production.
import nest_asyncio
nest_asyncio.apply()

celery_app = Celery("gain")

_always_eager = os.getenv("CELERY_TASK_ALWAYS_EAGER", "false").lower() == "true"

if _always_eager:
    # Dev mode: run tasks inline without SQS or workers
    celery_app.conf.update(
        task_always_eager=True,
        task_eager_propagates=True,
        broker_url="memory://",
        result_backend="cache",
        cache_backend="memory",
    )
else:
    celery_app.conf.update(
        # SQS broker — boto3 reads AWS credentials from environment
        broker_url="sqs://",
        broker_transport_options={
            "region": os.getenv("AWS_REGION", "ap-south-1"),
            "predefined_queues": {
                "voice":              {"url": os.getenv("SQS_VOICE_QUEUE_URL", "")},
                "whatsapp":           {"url": os.getenv("SQS_WHATSAPP_QUEUE_URL", "")},
                "email":              {"url": os.getenv("SQS_EMAIL_QUEUE_URL", "")},
                "zip":                {"url": os.getenv("SQS_ZIP_QUEUE_URL", "")},
                "ai-classification":  {"url": os.getenv("SQS_AI_CLASSIFICATION_QUEUE_URL", "")},
                "ai-extraction":      {"url": os.getenv("SQS_AI_EXTRACTION_QUEUE_URL", "")},
                "ai-tier1":           {"url": os.getenv("SQS_AI_TIER1_QUEUE_URL", "")},
                "ai-tier2":           {"url": os.getenv("SQS_AI_TIER2_QUEUE_URL", "")},
            },
            # Visibility timeout must be >= max task execution time (15 min)
            "visibility_timeout": 900,
        },
    )

celery_app.conf.update(
    # No result backend — fire-and-forget; results tracked in MongoDB WorkflowRuns
    result_backend=None if not _always_eager else "cache",
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Kolkata",
    enable_utc=True,
    # Acknowledge only after task completes — prevents losing tasks on worker crash
    task_acks_late=True,
    # One message at a time per worker process
    worker_prefetch_multiplier=1,
    task_track_started=True,
    # Celery Beat schedule — periodic tasks
    beat_schedule={
        "follow-up-hourly": {
            "task": "tasks.run_follow_up_check",
            "schedule": 3600.0,  # every hour
            "options": {"queue": "whatsapp"},
        },
    },
)

# Auto-discover tasks in workers package
celery_app.autodiscover_tasks(["app.workers"])
