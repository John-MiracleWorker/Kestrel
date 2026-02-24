import os
import re

source_path = "src/channels/discord.ts"
content = open(source_path, "r", encoding="utf-8").read()

def extract_block(text, start_marker, end_marker=None):
    start_idx = text.find(start_marker)
    if start_idx == -1:
        return "", text
    if end_marker:
        end_idx = text.find(end_marker, start_idx)
        if end_idx == -1:
            end_idx = len(text)
    else:
        end_idx = len(text)
    
    extracted = text[start_idx:end_idx]
    remaining = text[:start_idx] + text[end_idx:]
    return extracted, remaining

# Extract configuration and types -> types.ts
types_start = "// ── Configuration ──────────────────────────────────────────────────"
types_end = "// ── Slash Command Definitions ──────────────────────────────────────"
types_block, content = extract_block(content, types_start, types_end)

# Add "export " to interfaces in types_block
types_block = types_block.replace("interface DiscordUser", "export interface DiscordUser")
types_block = types_block.replace("interface DiscordMessagePayload", "export interface DiscordMessagePayload")
types_block = types_block.replace("interface DiscordInteraction", "export interface DiscordInteraction")

with open("src/channels/discord/types.ts", "w", encoding="utf-8") as f:
    f.write(types_block.strip() + "\\n")

# Extract slash commands and colors -> constants.ts
consts_start = "// ── Slash Command Definitions ──────────────────────────────────────"
consts_end = "// ── Discord Adapter ────────────────────────────────────────────────"
consts_block, content = extract_block(content, consts_start, consts_end)

consts_block = consts_block.replace("const SLASH_COMMANDS", "export const SLASH_COMMANDS")
consts_block = consts_block.replace("const COLORS", "export const COLORS")

with open("src/channels/discord/constants.ts", "w", encoding="utf-8") as f:
    f.write(consts_block.strip() + "\\n")

# Now change private to public in DiscordAdapter for the fields we need to access
content = content.replace("private isAllowed", "public isAllowed")
content = content.replace("private respondToInteraction", "public respondToInteraction")
content = content.replace("private startTyping", "public startTyping")
content = content.replace("private stopTyping", "public stopTyping")
content = content.replace("private resolveUserId", "public resolveUserId")
content = content.replace("private userChannelMap", "public userChannelMap")
content = content.replace("private channelUserMap", "public channelUserMap")
content = content.replace("private taskThreads", "public taskThreads")
content = content.replace("private pendingApprovals", "public pendingApprovals")
content = content.replace("private apiRequest", "public apiRequest")
content = content.replace("private config", "public config")

# Extract the handlers -> handlers.ts
handlers_start = "    private handleMessage(msg: DiscordMessagePayload): void {"
handlers_end = "    // ── Sending ────────────────────────────────────────────────────"

handlers_block, content = extract_block(content, handlers_start, handlers_end)

# Rewrite handlers_block to use adapter
handlers_block = handlers_block.replace("private handleMessage", "export function handleMessage(adapter: DiscordAdapter, msg: DiscordMessagePayload)")
handlers_block = handlers_block.replace("private async handleTaskMessage", "export async function handleTaskMessage(adapter: DiscordAdapter, msg: DiscordMessagePayload, goal: string)")
handlers_block = handlers_block.replace("private async handleInteraction", "export async function handleInteraction(adapter: DiscordAdapter, interaction: DiscordInteraction)")
handlers_block = handlers_block.replace("private async handleComponentInteraction", "export async function handleComponentInteraction(adapter: DiscordAdapter, interaction: DiscordInteraction)")

# Replace `this.` with `adapter.`
handlers_block = re.sub(r'\\bthis\\.', 'adapter.', handlers_block)

handlers_ts = f"""import {{ randomUUID }} from 'crypto';
import {{ IncomingMessage }} from '../base';
import {{ DiscordAdapter }} from './index';
import {{ DiscordInteraction, DiscordMessagePayload }} from './types';
import {{ COLORS }} from './constants';
import {{ logger }} from '../../utils/logger';

{handlers_block.strip()}
"""

with open("src/channels/discord/handlers.ts", "w", encoding="utf-8") as f:
    f.write(handlers_ts)

# Update index.ts (which is now discord.ts, but we will move it to discord/index.ts)
# Add imports
imports = """import { DiscordConfig, DiscordInteraction, DiscordMessagePayload } from './types';
import { SLASH_COMMANDS, COLORS } from './constants';
import { handleMessage, handleTaskMessage, handleInteraction, handleComponentInteraction } from './handlers';
"""

content = content.replace("import { logger } from '../utils/logger';", "import { logger } from '../../utils/logger';\\n" + imports)
content = content.replace("'./base'", "'../base'")

# Replace handlers in handleDispatch
content = content.replace("this.handleMessage(data as DiscordMessagePayload);", "handleMessage(this, data as DiscordMessagePayload);")
content = content.replace("this.handleInteraction(data as DiscordInteraction);", "handleInteraction(this, data as DiscordInteraction);")

with open("src/channels/discord/index.ts", "w", encoding="utf-8") as f:
    f.write(content)

os.remove("src/channels/discord.ts")
print("Discord refactor complete!")
