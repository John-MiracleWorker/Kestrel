---
description: Security implementations including sandboxing, access control, and risk management
---

# Security Model

## Tool Registry Access Control
Path: `packages/brain/agent/tools/__init__.py`
- Risk-level based tool access filtering
- Execution lifecycle management with timeouts
- Tool capability restrictions by security requirements

## Sandboxed Code Execution
Path: `packages/brain/agent/tools/code.py`
- Isolated execution environments per language
- Resource limits and timeout enforcement
- External service boundary controls

## Workspace Security Model
- Strict isolation between workspaces
- Encrypted credential storage
- Access control per workspace

## Risk Assessment Framework
Path: `packages/brain/agent/reflection.py`
- Systematic "red teaming" for plan validation
- Multi-stage critique with severity levels
- Risk mitigation requirement enforcement

## Guardrail System
Path: `packages/brain/agent/guardrails.py`
- Pattern matching for dangerous operations
- Rate limiting with adaptive thresholds
- Operation approval workflows

## Key Security Patterns
- No direct SQL access allowed
- Tool access controlled by risk level
- Mandatory workspace isolation
- Encrypted API key storage
- Sandboxed code execution
