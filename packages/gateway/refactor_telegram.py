import os
import re

source_path = "src/channels/telegram.ts"
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


# Extract types
types_start = "// ── Telegram API Types ─────────────────────────────────────────────"
types_end = "// ── Telegram Adapter ───────────────────────────────────────────────"
types_block, content = extract_block(content, types_start, types_end)

# Add export to interfaces
types_block = types_block.replace("interface TelegramUser", "export interface TelegramUser")
types_block = types_block.replace("interface TelegramChat", "export interface TelegramChat")
types_block = types_block.replace("interface TelegramMessage", "export interface TelegramMessage")
types_block = types_block.replace("interface TelegramUpdate", "export interface TelegramUpdate")

os.makedirs("src/channels/telegram", exist_ok=True)

with open("src/channels/telegram/types.ts", "w", encoding="utf-8") as f:
    f.write(types_block.strip() + "\n")


# Now change private to public in TelegramAdapter for the fields we need to access
content = content.replace("private readonly apiBase:", "public readonly apiBase:")
content = content.replace("private pollingActive", "public pollingActive")
content = content.replace("private pollingOffset", "public pollingOffset")
content = content.replace("private pollingTimer", "public pollingTimer")
content = content.replace("private chatIdMap", "public chatIdMap")
content = content.replace("private userIdMap", "public userIdMap")
content = content.replace("private typingIntervals", "public typingIntervals")
content = content.replace("private chatModes", "public chatModes")
content = content.replace("private pendingApprovals", "public pendingApprovals")
content = content.replace("private isAllowed", "public isAllowed")
content = content.replace("private startTyping", "public startTyping")
content = content.replace("private stopTyping", "public stopTyping")
content = content.replace("private escapeMarkdown", "public escapeMarkdown")
content = content.replace("private resolveUserId", "public resolveUserId")
content = content.replace("private resolveConversationId", "public resolveConversationId")
content = content.replace("private api(", "public api(")
content = content.replace("constructor(private config:", "constructor(public config:")

# Also make emit public by replacing it if we used adapter.emit -- actually base class emit is protected, we handled it with `(adapter as any).emit` in discord

# Extract handlers -> handlers.ts
handlers_start = "    // ── Webhook Handler ────────────────────────────────────────────"
handlers_end = "    // ── Polling ────────────────────────────────────────────────────"

handlers_block, content = extract_block(content, handlers_start, handlers_end)

# Rewrite functions in handlers_block
handlers_block = handlers_block.replace("async processUpdate(update: TelegramUpdate)", "export async function processUpdate(adapter: TelegramAdapter, update: TelegramUpdate)")
handlers_block = handlers_block.replace("private async handleTaskRequest(msg: TelegramMessage, from: TelegramUser, goal: string)", "export async function handleTaskRequest(adapter: TelegramAdapter, msg: TelegramMessage, from: TelegramUser, goal: string)")
handlers_block = handlers_block.replace("private async handleCommand(msg: TelegramMessage, text: string)", "export async function handleCommand(adapter: TelegramAdapter, msg: TelegramMessage, text: string)")
handlers_block = handlers_block.replace("private async handleApproval(chatId: number, approvalId: string, approved: boolean)", "export async function handleApproval(adapter: TelegramAdapter, chatId: number, approvalId: string, approved: boolean)")
handlers_block = handlers_block.replace("async sendApprovalRequest(chatId: number, approvalId: string, description: string, taskId: string)", "export async function sendApprovalRequest(adapter: TelegramAdapter, chatId: number, approvalId: string, description: string, taskId: string)")
handlers_block = handlers_block.replace("async sendTaskProgress(chatId: number, step: string, detail: string = '')", "export async function sendTaskProgress(adapter: TelegramAdapter, chatId: number, step: string, detail: string = '')")
handlers_block = handlers_block.replace("private async handleCallbackQuery(query:", "export async function handleCallbackQuery(adapter: TelegramAdapter, query:")

# Replace `this.` with `adapter.`
handlers_block = re.sub(r'\bthis\.', 'adapter.', handlers_block)

handlers_ts = f"""import {{ randomUUID }} from 'crypto';
import {{ IncomingMessage, Attachment }} from '../base';
import {{ TelegramAdapter }} from './index';
import {{ TelegramUpdate, TelegramMessage, TelegramUser }} from './types';
import {{ logger }} from '../../utils/logger';

{handlers_block.strip()}
"""

# Replace (adapter as any).emit
handlers_ts = handlers_ts.replace("adapter.emit(", "(adapter as any).emit(")

# Fix recursive calls
handlers_ts = handlers_ts.replace("adapter.handleCallbackQuery(", "handleCallbackQuery(adapter, ")
handlers_ts = handlers_ts.replace("adapter.handleCommand(", "handleCommand(adapter, ")
handlers_ts = handlers_ts.replace("adapter.handleTaskRequest(", "handleTaskRequest(adapter, ")
handlers_ts = handlers_ts.replace("adapter.handleApproval(", "handleApproval(adapter, ")


with open("src/channels/telegram/handlers.ts", "w", encoding="utf-8") as f:
    f.write(handlers_ts)

# Update index.ts (which is currently telegram.ts content)
imports = """import { TelegramConfig, TelegramUpdate, TelegramMessage, TelegramUser, TelegramChat } from './types';
import { processUpdate, handleTaskRequest, handleCommand, handleApproval, sendApprovalRequest, sendTaskProgress, handleCallbackQuery } from './handlers';
"""

content = content.replace("import { logger } from '../utils/logger';", "import { logger } from '../../utils/logger';\n" + imports)
content = content.replace("'./base'", "'../base'")

content = content.replace("await this.processUpdate(update);", "await processUpdate(this, update);")

with open("src/channels/telegram/index.ts", "w", encoding="utf-8") as f:
    f.write(content)

os.remove("src/channels/telegram.ts")
print("Telegram refactor complete!")
