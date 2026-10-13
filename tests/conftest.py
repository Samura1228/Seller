import os
import tempfile

# Settings for tests. DB tests are skipped unless TEST_DATABASE_URL points to a disposable Postgres.
os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("TG_API_ID", "1")
os.environ.setdefault("TG_API_HASH", "test")
os.environ.setdefault("ADMIN_TG_ID", "1")
os.environ.setdefault("SELLER_USERNAME", "@limassol_seller")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("APIFY_TOKEN", "test")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="seller-test-"))
os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", "postgresql://invalid/none"))
