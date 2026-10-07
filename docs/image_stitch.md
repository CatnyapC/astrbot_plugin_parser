# Image Stitch Command

`/parserimg stitch --source latest --direction horizontal|vertical` stitches the
latest successfully sent multi-image group in the current group. Defaults are
latest/horizontal. Reply/selected sources are unsupported; cache is in memory.

The service reuses the existing platform raw-id reader and attaches
`extra.reply_raw_id` to the result. The shared sender adds a native Reply outside
merged-forward nodes, retaining it during image retries. Failure text also
replies to the command. Router can therefore associate the downstream receipt
with the archived command's worker_task_id instead of guessing from image timing.

Parser owns sending the image; Router's V2 worker only confirms the correlated
result through mainLLM and must not resend it. Group members can use stitch;
parserwl retains its separate authorization rules.
