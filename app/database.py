from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from app.config import settings
import certifi
import logging

logger = logging.getLogger("gain.database")

client: AsyncIOMotorClient | None = None
db: AsyncIOMotorDatabase | None = None


async def connect_to_mongo() -> None:
    global client, db
    import asyncio

    # Diagnostic: log connection string (masked) and DNS availability
    url = settings.MONGODB_URL
    is_srv = url.startswith("mongodb+srv://")
    masked = url[:20] + "***" + url[-30:] if len(url) > 50 else "***"
    logger.info(f"Connecting to MongoDB... SRV={is_srv} db={settings.MONGODB_DATABASE} url={masked}")

    # Check dnspython is available (required for mongodb+srv://)
    if is_srv:
        try:
            import dns.resolver
            logger.info("dnspython available — SRV resolution OK")
        except ImportError:
            logger.error("dnspython NOT installed — mongodb+srv:// will fail! pip install dnspython")

    client = AsyncIOMotorClient(
        url,
        maxPoolSize=10,
        minPoolSize=1,
        serverSelectionTimeoutMS=30000,  # 30s — Railway cold starts can be slow
        connectTimeoutMS=20000,
        socketTimeoutMS=20000,
        tlsCAFile=certifi.where(),  # Fix macOS SSL cert verification
    )
    db = client[settings.MONGODB_DATABASE]

    # Verify connection with retries (Railway IP changes can cause transient failures)
    for attempt in range(3):
        try:
            await client.admin.command("ping")
            logger.info(f"Connected to MongoDB database: {settings.MONGODB_DATABASE}")
            break
        except Exception as exc:
            if attempt < 2:
                logger.warning(f"MongoDB connection attempt {attempt + 1}/{3} failed: {exc} — retrying in 5s...")
                await asyncio.sleep(5)
            else:
                logger.error(f"MongoDB connection failed after 3 attempts: {exc}")
                raise

    await _create_indexes()


async def close_mongo_connection() -> None:
    global client
    if client:
        client.close()
        logger.info("MongoDB connection closed")


async def _create_indexes() -> None:
    # Tenants
    await db.tenants.create_index([("is_active", 1)])

    # Users
    await db.users.create_index([("email", 1)], unique=True)
    await db.users.create_index([("tenant_id", 1), ("is_active", 1)])

    # Refresh tokens — TTL index auto-expires documents
    await db.refresh_tokens.create_index([("token_hash", 1)], unique=True)
    await db.refresh_tokens.create_index([("expires_at", 1)], expireAfterSeconds=0)
    await db.refresh_tokens.create_index([("user_id", 1)])

    # Leads
    await db.leads.create_index([("tenant_id", 1), ("status", 1)])
    await db.leads.create_index([("tenant_id", 1), ("campaign_id", 1)])
    await db.leads.create_index([("tenant_id", 1), ("assigned_to", 1)])
    await db.leads.create_index([("tenant_id", 1), ("created_at", -1)])

    # Campaigns
    await db.campaigns.create_index([("tenant_id", 1), ("status", 1)])

    # Agents
    await db.agents.create_index([("tenant_id", 1), ("type", 1), ("status", 1)])

    # Physical files
    await db.phys_files.create_index([("lead_id", 1), ("tenant_id", 1)])
    await db.phys_files.create_index([("tenant_id", 1), ("status", 1)])

    # Logical docs
    await db.logical_docs.create_index([("lead_id", 1), ("tenant_id", 1)])
    await db.logical_docs.create_index([("lead_id", 1), ("doc_type", 1)])
    await db.logical_docs.create_index([("tenant_id", 1), ("status", 1)])

    # Workflow runs
    await db.workflow_runs.create_index([("lead_id", 1), ("tenant_id", 1)])
    await db.workflow_runs.create_index([("tenant_id", 1), ("executed_at", -1)])
    await db.workflow_runs.create_index([("campaign_id", 1), ("status", 1)])

    # Activity feed
    await db.activity_feed.create_index([("tenant_id", 1), ("created_at", -1)])

    # Calls — one record per outbound call attempt (PRD Section 5.8)
    await db.calls.create_index([("lead_id", 1), ("tenant_id", 1)])
    await db.calls.create_index([("tenant_id", 1), ("initiated_at", -1)])
    # sparse=True allows multiple null values (failed calls with no conversation_id)
    await db.calls.create_index(
        [("elevenlabs_conversation_id", 1)], unique=True, sparse=True
    )

    # WhatsApp messages — one record per WA message sent/received (PRD Section 5.9)
    await db.whatsapp_messages.create_index([("lead_id", 1), ("tenant_id", 1)])
    await db.whatsapp_messages.create_index([("lead_id", 1), ("sent_at", 1)])
    await db.whatsapp_messages.create_index([("tenant_id", 1), ("direction", 1)])

    # Lead mobile_hash — SHA256 of normalized mobile for WhatsApp sender matching
    await db.leads.create_index([("tenant_id", 1), ("mobile_hash", 1)], sparse=True)
    await db.leads.create_index([("mobile_hash", 1)], sparse=True)  # For direct lookup without tenant

    # Activity feed — also index by lead_id for Activity tab queries
    await db.activity_feed.create_index([("lead_id", 1), ("tenant_id", 1), ("created_at", -1)])

    # Email messages
    await db.email_messages.create_index([("lead_id", 1), ("tenant_id", 1)])

    # Webhook idempotency
    await db.webhook_idempotency.create_index([("conversation_id", 1)], unique=True)

    # Lead pan_hash — SHA256 of uppercased PAN for deduplication (sparse: leads without PAN are exempt)
    try:
        await db.leads.create_index(
            [("tenant_id", 1), ("pan_hash", 1)], unique=True, sparse=True
        )
    except Exception as exc:
        # Index may already exist with different options; log and continue
        logger.warning(f"Could not create pan_hash index (may already exist): {exc}")

    logger.info("MongoDB indexes created")


def get_db() -> AsyncIOMotorDatabase:
    return db
