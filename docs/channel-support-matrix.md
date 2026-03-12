# Channel Support Matrix

| Channel  | Ingress contract status             | Delivery mode                                 | Auth model                           | Current maturity        | Notes                                                                                                                                          |
| -------- | ----------------------------------- | --------------------------------------------- | ------------------------------------ | ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| Web      | Native `NormalizedIngressEvent`     | WebSocket                                     | JWT over WebSocket auth              | Usable but experimental | Strongest interactive surface. Still needs operator views and richer observability.                                                            |
| Telegram | Native `NormalizedIngressEvent`     | Webhook or polling                            | Bot token plus channel allowlist     | Usable but experimental | Chat, task, command, and callback entrypoints now normalize at ingress. Approval and richer orchestration still live inside the adapter layer. |
| Discord  | Native `NormalizedIngressEvent`     | Gateway WebSocket plus Discord API            | Bot token, guild config, role gating | Usable but experimental | Gateway messages, task-thread starts, slash commands, and component callbacks now normalize at ingress.                                        |
| WhatsApp | Native `NormalizedIngressEvent`     | Twilio webhook                                | Twilio credentials                   | Usable but experimental | Webhook chat, task, and command paths now normalize at ingress, but channel depth still trails web and Telegram.                               |
| Mobile   | No first-class ingress contract yet | Push registration and sync helper routes only | JWT for helper routes                | Partially implemented   | Treat as experimental until there is a real session, sync, offline, and artifact model.                                                        |

## Hardening order

1. Web
2. Telegram
3. Discord
4. WhatsApp
5. Mobile
