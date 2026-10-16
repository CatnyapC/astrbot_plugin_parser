# Parser media fingerprint push

Set `receipt_url` and `receipt_token` to push media fingerprints and source text
to Foxmi's existing `/api/parser/receipts` listener. Empty settings disable push.
Use HTTPS or loopback HTTP carried by SSH. Redirects are refused. Tailscale and
existing secrets need no change.

Ordinary QQ parsing establishes source context only for its current send task.
Immediately before sending each chain, SHA-256 is computed from the actual local
media files, including nested forward nodes, preview images and image retries.
The existing downloaded file is reused. No media, path or credential is pushed.
Text-only outputs and media-operation commands do not create share receipts.

Before QQ dispatch, write source, content metadata, file fingerprints, UUID and
`created_at` nanosecond timestamp atomically to `receipts/<UUID>.json` (0600).
The background worker posts that exact JSON, retries failures up to a 60-second
backoff and deletes only after durable `accepted`. Restart needs no QQ adapter
or message lookup. QQ ACK timeout does not suppress metadata, and HTTP retries
never resend media. Normal media send policies remain in the sender.

Metadata retains original sharer, original link, title, body, author, publication
information, durations and Bilibili's successful platform AI summary. Existing
YouTube/TikTok descriptions need no extra fetch. Fields stay capped at 16,000
characters. QQ JSON cards preserve the full extracted URL; display cleanup stays
on Foxmi's timeline.

Deploy with the matching Thread Archive version. The protocol now uses
`fingerprints` and `created_at`, replacing `deliveries` and native-order lookup;
there is no old-format fallback or core send-receipt prerequisite. Check/drain
old-format queue files before deployment; no automatic conversion is performed.

Archive keeps the latest source for each group/account/file digest, consumes it
when attached, ignores late older retries and leaves already-bound messages
unchanged. Receiver arrival order does not matter. Full contract and deployment:
Thread Router `docs/media/parser_receipt_push.md`.
