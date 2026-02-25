# Kestrel System Fix & Integration Log
**Date:** 2026-02-25
**Status:** Partial Integration / Blocked by Environment Constraints

This document outlines the specific manual steps required to synchronize the local environment, fix tool registration, and bypass the constraints that blocked Kestrel's autonomous progress.

---

## 1. Git & Branch Synchronization
Kestrel is currently blocked by a **200-line per commit limit**. You can bypass this manually.

**Current State:**
- **Branch:** `kestrel/system-tools-v2`
- **Staged File:** `packages/brain/agent/tools/git.py` (344 lines of changes).

**Action Required:**
1. Open your terminal in the project root (`/Users/tiuni/little bird alt`).
2. Run the following to commit the changes Kestrel prepared:
   ```bash
   git commit -m "feat: enhance git tool and add system monitoring logic"
   ```
3. Sync with your main remote branch:
   ```bash
   git checkout main
   git merge kestrel/system-tools-v2
   git push origin main
   ```

---

## 2. Dependency & Tool Registration
Kestrel wrote several new tools, but they won't be "live" until the Brain service is updated and rebuilt.

**Action Required:**
1. **Update Requirements:** Ensure `psutil` is in `packages/brain/requirements.txt`.
2. **Register Tools:** Check `packages/brain/agent/tools/__init__.py`. It needs to import and register the new modules:
   - `system_health.py`
   - `process_manager.py`
   - `moltbook_reporter.py`
   - `system_tools.py`
3. **Manual Rebuild:** Since Kestrel's `container_control` cannot find Docker, run this from your host terminal:
   ```bash
   docker-compose up --build brain
   ```

---

## 3. Gmail MCP Server Setup
The Gmail server logic is written to `mcp-servers/gmail/server.py`, but it lacks credentials.

**Action Required:**
1. Go to [Google Cloud Console](https://console.cloud.google.com/).
2. Enable **Gmail API**.
3. Create **OAuth 2.0 Client ID** (Desktop App).
4. Download the JSON and save it as:
   `/Users/tiuni/little bird alt/mcp-servers/gmail/credentials.json`
5. Run the server once manually to complete the OAuth flow:
   ```bash
   python3 mcp-servers/gmail/server.py
   ```

---

## 4. Environment Path Issues
Kestrel's `code_execute` (sandboxed) and `container_control` tools are currently unable to see `git` or `docker` in their `$PATH`.

**Investigation Required:**
- Verify if Docker is installed as a Desktop app and if the CLI tools are linked in `/usr/local/bin`.
- Kestrel may need the full path to the binaries (e.g., `/usr/local/bin/docker-compose`) hardcoded into the tools if the environment variables aren't propagating.

---

## 5. API Quota (Computer Use)
The `computer_use` tool is currently failing with **Error 400 (Quota Exhausted)**. 

**Action Required:**
- Wait for the Gemini API quota to reset, or upgrade the API key tier if this is a recurring issue during high-intensity development.

---

## Summary of Files Created by Kestrel
If you need to verify the code Kestrel wrote, look at these paths:
- `packages/brain/agent/tools/system_health.py`
- `packages/brain/agent/tools/process_manager.py`
- `packages/brain/agent/tools/moltbook_reporter.py`
- `packages/brain/agent/tools/system_tools.py`
- `mcp-servers/gmail/server.py`
