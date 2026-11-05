# Owner Intake Adapter Contract

English | [Chinese](owner-intake.zh-CN.md)

An external channel adapter has two jobs: write a signed request file, and provide a command that delivers Owner updates. The request is **untrusted data, never dispatch authorization**. The channel must verify the Owner's identity before writing it. The Planner may dispatch only after the Owner gives direct approval in the Planner's own session.

## Request file

Write `<inbox>/<request_id>.json` as UTF-8 JSON. Create a temporary file in the same directory (name starting with `.` or ending with `.tmp`), then rename it to the final name. AWO ignores temporary files, other extensions, and subdirectories. The adapter creates the inbox. Use a durable, collision-free ID matching `[A-Za-z0-9._-]{1,64}`; AWO does not impose a channel prefix. For example, a QQ adapter can use `qq-20261105-0007`, persist its daily sequence separately, and refuse an ID already present in the inbox or `processed/`.

The [JSON schema](owner-intake.schema.json) describes schema 1. Required fields are `schema: 1`, `request_id` (equal to the filename stem), `channel` (record only), `text`, `requested_at`, `confirmed_at`, `source`, and `hmac`. `project` is optional; include it only when the Owner named a project. It must equal that project's workflow `name`. The `source` object contains an adapter-specific `kind`, `owner: true`, and a 16-character lowercase hex `message_ref` derived from the original message ID. Do not put the original ID in the file.

The `text` is the body after removing the adapter prefix and project name. It must have 1-4000 Unicode code points (Python `len`, JavaScript `[...text].length`). Tabs and newlines are allowed; other control characters are not. Times use ISO 8601 with a numeric offset, whole seconds and no milliseconds, for example `2026-11-05T10:00:30+08:00`. AWO also accepts `Z`.

Read `AWO_INTAKE_HMAC_KEY` from the environment only. Sign the UTF-8 bytes of `request_id`, `text`, `requested_at`, `confirmed_at`, and `project` (empty when absent), joined in that order with LF. Write lowercase hex HMAC-SHA256. AWO compares it in constant time and refuses unsigned or changed files. Keep the key out of JSON, logs, and repository content.

## Sending Owner updates

Configure an explicit `intake_update` kind in the selected `owner_notify.py` command channel. The argv array contains exactly one standalone `{text}` element and never uses a shell. Exit 0 means sent, or safely queued for later delivery; a nonzero exit causes AWO's at-least-once outbox to retry. For a queueing HTTP bridge, a successful immediate response can be HTTP 200, and a durable queued response HTTP 202; the bridge should finish within 10 seconds (AWO command timeout: 15 seconds). Deduplicate `sha256(text)` for one hour on the channel side to absorb retries. AWO limits the redacted message body to 300 characters.

The request's `channel` does not route replies. Only `owner_intake.notify_channel` in the project's workflow config selects the destination. [Example workflow section](../examples/owner-intake.workflow-snippet.json) and [example command channel](../examples/notify-qq.json) use placeholder paths. The same contract works for QQ, Telegram, email, or another authenticated channel; no bot-specific implementation is required in this repository.
