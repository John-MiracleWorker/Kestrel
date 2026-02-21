---
description: Memory and knowledge management systems including vector storage, semantic search, and persistence mechanisms
---

# Memory System

## Core Memory Architecture
Two-tier memory system implementation:

### Short-Term Conversation Memory
- Semantic vector storage for recent interactions
- Time-weighted relevance scoring with 10% weekly decay
- Automatic conversation embedding with background processing

### Long-Term Knowledge Persistence
- Workspace-scoped information storage
- Vector-based similarity search for context retrieval
- Metadata-enriched embedding storage using HNSW indices

## Semantic Search Implementation
- Time-weighted relevance scoring algorithm for memory retrieval
- Custom decay function applied to older memories
- Context formatting and automatic prompt augmentation
- Background worker for asynchronous embedding processing

## Memory Graph System
- Semantic knowledge graph connecting conversation entities
- Time-based decay system for node relevance
- Entity relationship modeling specific to agent conversations
- Custom traversal algorithm for context retrieval

## Memory Persistence
- Workspace isolation for memory storage
- Conversation turn processing (Q&A pairs)
- Automatic conversation embedding
- Metadata-enriched vector storage

## Key Interactions
- Memory Graph feeds contextual data to semantic search
- Short-term memory automatically transitions to long-term storage
- Time-based decay affects both tiers of memory
- Workspace scoping enforced across all memory operations
