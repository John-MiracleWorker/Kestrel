import os

filepath = "src/components/Chat/ChatView.tsx"
with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

kbar_start = content.find("/* ── KestrelProcessBar ────────────────────────────────────────────── */")
fbtns_start = content.find("/* ── Feedback Buttons ─────────────────────────────────────────────── */")

if kbar_start == -1 or fbtns_start == -1:
    print("Could not find sections!")
    exit(1)

process_bar_content = content[kbar_start:fbtns_start]
message_bubble_content = content[fbtns_start:]
chat_view_content = content[:kbar_start]

# MessageBubble.tsx
message_bubble_full = """import { useState } from 'react';
import { RichContent } from './RichContent';
import { ActivityTimeline } from '../Agent/ActivityTimeline';
import { KestrelProcessBar } from './KestrelProcessBar';

""" + message_bubble_content

message_bubble_full = message_bubble_full.replace("function MessageBubble", "export function MessageBubble")
message_bubble_full = message_bubble_full.replace("function FeedbackButtons", "export function FeedbackButtons")

# KestrelProcessBar.tsx
process_bar_full = """import React from 'react';

""" + process_bar_content

process_bar_full = process_bar_full.replace("function KestrelProcessBar", "export function KestrelProcessBar")
process_bar_full = process_bar_full.replace("function PhaseDetail", "export function PhaseDetail")

# ChatView.tsx
chat_view_imports = """import { MessageBubble } from './MessageBubble';
"""
chat_view_content = chat_view_content.replace(
    "import { InputComposer } from './InputComposer';",
    "import { InputComposer } from './InputComposer';\n" + chat_view_imports
)
chat_view_content = chat_view_content.replace("import { ActivityTimeline } from '../Agent/ActivityTimeline';\n", "")

with open("src/components/Chat/MessageBubble.tsx", "w", encoding="utf-8") as f:
    f.write(message_bubble_full)
with open("src/components/Chat/KestrelProcessBar.tsx", "w", encoding="utf-8") as f:
    f.write(process_bar_full)
with open(filepath, "w", encoding="utf-8") as f:
    f.write(chat_view_content)

print("ChatView refactor complete!")
