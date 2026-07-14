import os


# Offline tests inject fake clients but production code still routes by a
# configured semantic model name. Keep the test environment explicit.
os.environ.setdefault("ATTUNE_MODEL_DEFAULT", "test-model")
os.environ.setdefault("ATTUNE_EMBEDDING_MODEL", "test-embedding")
os.environ.setdefault("ATTUNE_EMBEDDING_DIMENSIONS", "1536")
