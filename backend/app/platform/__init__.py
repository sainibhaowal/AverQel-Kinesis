"""Platform infrastructure shared by all application features."""

from app.platform.embedded_redis import setup_embedded_redis

# Auto-initialize embedded redis compatibility for local-first operations
setup_embedded_redis()
