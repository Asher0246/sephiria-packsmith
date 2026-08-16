import os
import tempfile

# Keep the on-disk result cache used by the server hermetic during tests.
os.environ["SEPHIRIA_CACHE_DIR"] = tempfile.mkdtemp(prefix="sephiria-cache-test-")
