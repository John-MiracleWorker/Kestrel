# Gateway Ingress Contract

The canonical Gateway ingress type is `NormalizedIngressEvent` in `packages/gateway/src/channels/ingress.ts`.

## Rollout status

- Web now emits the canonical contract directly.
- Telegram, Discord, and WhatsApp now emit the canonical contract directly from their adapter entrypoints.
- Remaining legacy adapters still emit `IncomingMessage`.
- `ChannelRegistry` upgrades any remaining legacy adapter messages into `NormalizedIngressEvent` so downstream routing has one internal shape while the migration finishes.

## Required fields

| Field                    | Purpose                                                                                          |
| ------------------------ | ------------------------------------------------------------------------------------------------ |
| `channel`                | Internal channel identifier such as `web`, `telegram`, or `discord`.                             |
| `userId`                 | Resolved Kestrel user id used for Brain routing.                                                 |
| `workspaceId`            | Workspace ownership and policy scope.                                                            |
| `conversationId`         | Internal conversation id used for Brain persistence when available.                              |
| `externalUserId`         | Original channel user identifier.                                                                |
| `externalConversationId` | Original external conversation, chat, or thread container id.                                    |
| `externalThreadId`       | Optional external thread or topic id.                                                            |
| `content` and `payload`  | The normalized user payload. `payload.kind` currently supports `message`, `task`, and `command`. |
| `attachments`            | Normalized attachment descriptors before adapter-specific handling.                              |
| `metadata`               | Required channel metadata, including `channelUserId`, `channelMessageId`, and `timestamp`.       |
| `dedupeKey`              | Stable key for duplicate suppression and traceability.                                           |
| `correlationId`          | Request trace id propagated into Brain parameters.                                               |
| `authContext`            | How the Gateway authenticated or resolved the actor.                                             |
| `rawMetadata`            | Original adapter or transport metadata preserved for debugging.                                  |

## Shared Brain request builder

`buildBrainStreamChatRequest()` is the only helper that should translate ingress events into Brain `StreamChat` requests. It is now used by:

- the web adapter streaming path
- the channel registry path for legacy adapters

This keeps channel-specific routing metadata, dedupe keys, attachments, and correlation ids consistent while the remaining adapters are migrated.
