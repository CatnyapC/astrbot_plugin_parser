# Parser metadata receipt push

Set `receipt_url` and `receipt_token` to push acknowledged ordinary QQ link-parse
deliveries to Foxmi's `/api/parser/receipts` listener. Empty settings disable push.
Use HTTPS or a loopback HTTP endpoint carried by SSH; redirects are disabled.
No media files, local paths or cookies are included in receipts.

The original parser send plan, preview settings and native message format remain
unchanged. Captured per-message acknowledgements include partial successful
sends. An ACK timeout never fabricates a confirmed delivery. Source metadata
and acknowledged raw IDs are saved atomically to
`data/plugin_data/astrbot_plugin_parser/receipts/<UUID>.json` with mode 0600.
The background worker obtains metadata for those acknowledged messages, then
pushes JSON. It retains files on errors, retries with backoff up to 60 seconds,
and deletes a receipt only after a durable `accepted` response. Restarted workers
can resolve the saved adapter without waiting for new group traffic. Push failure
never retries the QQ media send.

Receipts carry original sharer, input link, publication information, title,
description/body, creator name and available durations. Bilibili's successful
platform AI summary is separate from its description; error/status placeholders
are omitted. Existing YouTube/TikTok downloader descriptions are retained in
`ParseResult.text`, alongside the source URL. No new ASR, subtitle or AI service
is called. Text is bounded to 16,000 characters per field.

Delivery matching requires native NapCat group order and actual platform time
from the acknowledged message. Remote client short raw IDs are diagnostic only.
Missing identity keeps the receipt pending; group/sender mismatches are rejected.
The initial transport is deliberately QQ-only; media-operation commands and
Telegram do not create ordinary share receipts.

Deploy Thread Router and Thread Archive with the matching receiver changes.
Full tunnel setup and the receipt contract live in Router's
`docs/media/parser_receipt_push.md`. The public-QQ live matching proof remains a
deployment check. A crash between QQ acknowledgement and local durable write can
lose provenance; that gap must never trigger media resending.

## Unacknowledged send attempts

Ordinary QQ parse sends bind a local UUID to each exact outgoing message chain.
On a OneBot sendMsg ACK timeout, or a local send deadline, the same outbox saves
source/content metadata plus an `awaiting_confirmation` attempt under
`receipts/<UUID>.json` (0600). The attempt records its local ID, platform/account
and group through `source`, start/timeout times, component types, and any earlier
acknowledged IDs. It contains no media paths or media bytes. Timeout handling
never retries the media, even if pending-file persistence fails; that failure is
logged separately. Ordinary acknowledged sends keep the existing enqueue path.

Only the platform acknowledgment hook (`record_sent`) on that exact chain can
release its attempt. This uses the existing core confirmation boundary, not an
incoming-chat listener, a history search, or a public API accepting guessed IDs.
An unrelated chain, an empty ACK, an ACK containing multiple candidate IDs, or
an already acknowledged prefix cannot confirm it. Repeated confirmations reuse
the original UUID and cannot recreate an accepted/deleted file. The worker then
queries the local adapter's message ID and verifies group, sending account,
positive native `real_seq`, and platform timestamp before constructing
`deliveries` and performing HTTP delivery. Adapter reuse is scoped by platform,
account, and group, so IDs are not borrowed from another client. Local attempt
fields are excluded from the HTTP payload. HTTP failure retains the file;
only a 200 response with `status=accepted` deletes it.

**Current NapCat boundary:** its sendMsg timeout provides no reliable native ID
or attempt token, and no late ACK is routed back to the timed-out chain. Such
attempts therefore remain unconfirmed, including across restarts. Restarted
workers retain them without querying recent history or sending HTTP. There is no
new database, receiver, or polling worker. A future adapter may complete an
attempt only if it can route a genuine acknowledgment to the original bound
chain; a restored attempt needs equally reliable platform binding, which is not
currently implemented. Time adjacency, latest message, another client's raw ID,
title, size, and duration are never confirmation evidence. Old incidents are not
backfilled automatically.

Logs distinguish `awaiting_confirmation`, `confirmation_failed`,
`enrichment_failed`, `delivery_failed` (HTTP status where available), and
`persistence_failed`, using UUIDs/error classes without credentials or URLs.

Regression command: `uv run pytest tests/test_receipts.py tests/test_sender.py -q`.
Transport and late-confirmation tests use mocks; they are not evidence of live
NapCat late-ACK support.
