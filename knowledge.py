"""
Libre Bird — Local Knowledge Base (RAG)
Vector-based semantic search over user documents using ChromaDB.
All data stays on-device — embeddings are computed locally.
"""

import logging
import os
import hashlib
from typing import Optional

logger = logging.getLogger("libre_bird.knowledge")

# Lazy-loaded ChromaDB client
_client = None
_collection = None

KNOWLEDGE_DIR = os.path.expanduser("~/.libre-bird/knowledge")
COLLECTION_NAME = "libre_bird_docs"
CHUNK_SIZE = 500  # characters per chunk
CHUNK_OVERLAP = 50


def _get_collection():
    """Get or create the ChromaDB collection (lazy init)."""
    global _client, _collection
    if _collection is not None:
        return _collection

    try:
        import chromadb
        os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
        _client = chromadb.PersistentClient(path=KNOWLEDGE_DIR)
        _collection = _client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(f"Knowledge base loaded: {_collection.count()} documents")
        return _collection
    except Exception as e:
        logger.error(f"Failed to initialize knowledge base: {e}")
        return None


def _chunk_text(text: str, chunk_size: int = CHUNK_SIZE,
                overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping chunks."""
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk.strip())
        start = end - overlap
    return chunks


def add_document(text: str, source: str = "user_input",
                 metadata: Optional[dict] = None) -> dict:
    """
    Add a document to the knowledge base.

    Args:
        text: The document text to index
        source: Source identifier (filename, URL, etc.)
        metadata: Optional metadata dict
    """
    collection = _get_collection()
    if collection is None:
        return {"error": "Knowledge base not available"}

    chunks = _chunk_text(text)
    if not chunks:
        return {"error": "No content to index"}

    # Generate deterministic IDs based on content
    ids = []
    documents = []
    metadatas = []

    for i, chunk in enumerate(chunks):
        chunk_hash = hashlib.md5(f"{source}:{i}:{chunk}".encode()).hexdigest()
        ids.append(chunk_hash)
        documents.append(chunk)
        meta = {"source": source, "chunk_index": i}
        if metadata:
            meta.update(metadata)
        metadatas.append(meta)

    collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
    logger.info(f"Indexed {len(chunks)} chunks from '{source}'")

    return {
        "status": "indexed",
        "source": source,
        "chunks": len(chunks),
        "total_documents": collection.count(),
    }


def search(query: str, top_k: int = 5) -> dict:
    """
    Search the knowledge base semantically.

    Args:
        query: Natural language search query
        top_k: Number of results to return
    """
    collection = _get_collection()
    if collection is None:
        return {"error": "Knowledge base not available"}

    if collection.count() == 0:
        return {"results": [], "message": "Knowledge base is empty. Add documents first."}

    results = collection.query(
        query_texts=[query],
        n_results=min(top_k, collection.count()),
    )

    formatted = []
    for i, doc in enumerate(results["documents"][0]):
        meta = results["metadatas"][0][i] if results["metadatas"] else {}
        distance = results["distances"][0][i] if results["distances"] else None
        formatted.append({
            "content": doc,
            "source": meta.get("source", "unknown"),
            "relevance": round(1 - distance, 3) if distance is not None else None,
        })

    return {"results": formatted, "total_indexed": collection.count()}


def list_sources() -> dict:
    """List all unique sources in the knowledge base."""
    collection = _get_collection()
    if collection is None:
        return {"error": "Knowledge base not available"}

    if collection.count() == 0:
        return {"sources": [], "total_documents": 0}

    # Get all metadata to extract unique sources
    all_data = collection.get(include=["metadatas"])
    sources = {}
    for meta in all_data["metadatas"]:
        src = meta.get("source", "unknown")
        sources[src] = sources.get(src, 0) + 1

    return {
        "sources": [{"name": k, "chunks": v} for k, v in sources.items()],
        "total_documents": collection.count(),
    }
