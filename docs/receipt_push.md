# Parser metadata receipt push

Set `receipt_url` and `receipt_token` to push acknowledged ordinary QQ link-parse
deliveries to Foxmi's `/api/parser/receipts` listener. Empty settings disable push.
Use HTTPS or a loopback HTTP endpoint carried by SSH; redirects are disabled.
No media files, local paths or cookies are included in receipts.

The original parser send plan, preview settings and native message format remain
unchanged. Captured per-message acknowledgements include partial successful
sends; no receipt is invented for an unacknowledged timeout. After a send attempt,
source metadata and acknowledged raw IDs are saved atomically to
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

QQ JSON cards retain the entire URL returned by `extract_json_url` in
`source.link`, including scheme, hostname, query parameters and fragment. Parser
matching and link debouncing still use the parser's regex match; that match may
be only a resource ID or a relative path and is not the card's original URL.
The full card URL survives outbox persistence, restart and HTTP delivery without
display cleanup. Existing receipts are not rewritten.

Foxmi's timeline separately prefers an available recognized platform short URL;
otherwise it removes only known tracking parameters for display. Content IDs,
page/time selectors, access tokens and unknown parameters remain. It does not
request a new short URL. The full original URL stays in the receipt for audit.

2026-10-17 validation: all 155 Parser tests passed, including QQ card dict/JSON
inputs and full-link persistence through failed delivery and restart. Changed
Python files pass Ruff. No remote deployment or old-receipt rewrite ran.

Deploy Thread Router and Thread Archive with the matching receiver changes.
Full tunnel setup and the receipt contract live in Router's
`docs/media/parser_receipt_push.md`. The public-QQ live matching proof remains a
deployment check. A crash between QQ acknowledgement and local durable write can
lose provenance; that gap must never trigger media resending.
