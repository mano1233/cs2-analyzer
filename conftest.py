"""cluster_run reads its configuration at import, so the suite supplies dummies."""
import os

os.environ.setdefault("R2_BUCKET", "test-bucket")
os.environ.setdefault("R2_ENDPOINT", "http://test-endpoint")
