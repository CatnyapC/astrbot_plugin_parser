# Video GIF Command

`/parserclip gif --source latest|reply --cycle off|auto` converts a cached or
quoted video to a real, infinitely looping GIF image without audio. Defaults:
`source=latest`, `cycle=off`. No other options are accepted.

- `latest`: latest video in the current group's existing video cache.
- `reply`: exact native quote of the original video or parser video output.
  Missing/ambiguous quotes fail; no fallback to another cached video. Uncached
  native OneBot/Telegram video quotes reuse slice's group checks, media download
  limits and cache registration.
- `off`: full video, preserving its content. Oversized/overlong videos are
  rejected, never silently trimmed.
- `auto`: shortest reliably verified complete repeating unit, then infinite GIF
  playback. At least two periods must fit, with all subsequent sampled frames
  agreeing and the wrap seam matching normal frame transitions. Static or
  nonperiodic content returns `cycle_not_found`.

Fixed limits: input 60 seconds / 100 MiB; GIF 12 FPS, longest side at most 480
pixels, output at most 10 MiB and the configured media size limit.
Output size is checked after full encoding; oversized GIFs are removed rather
than accepting an early truncated result. Temporary output can exceed 10 MiB.
Process timeout is 120 seconds; ffprobe and native downloads retain their existing slice
limits. The shared slice/GIF allowance is 30 attempts per hour. Duplicate command
raw IDs are suppressed after successful send; GIF requests execute serially.
Temporary samples and failed outputs are removed. Successful GIFs use the
existing cache cleanup lifecycle.

Cycle detection samples 32x32 grayscale frames at 12 FPS. It supports periods
of at least 0.25 seconds at 1/12-second resolution, including two source frames
displayed for 125 ms each. Low-contrast, very short,
off-grid or significantly edited repeats can be rejected. It does not infer
semantic repetition or remove titles/intros.

The existing `parser_video_slice_controller_ids`, AstrBot admins, parser's own
account and event admin permission control backend access. Router may request
the operation on behalf of ordinary group members through its controller
account; ordinary members cannot bypass parser's controller check.

Success GIFs carry `extra.reply_raw_id` for the exact command. All failure and
duplicate receipts also natively quote that command. Router's worker can bind
the receipt and report confirmed success without resending the image. Common
failures use `parserclip gif 失败: <reason>`, including `invalid_arguments`,
`reply_required`, `reply_source_not_found`, `permission_denied`,
`duration_too_long`, `input_too_large`, `output_too_large`, `cycle_not_found`,
`ffmpeg_timeout`, `ffmpeg_failed` and `send_failed`.

Regression tests include synthetic repeat/static/nonperiodic samples, strict
quote selection, command validation, permission/duration rejection, native
failure receipts, and real ffmpeg-generated GIFs sent through the shared sender.
An 8 FPS AB source loop produces three 12 FPS samples with GIF delays of
80/90/80 ms: one complete 250 ms cycle, then infinite playback.
