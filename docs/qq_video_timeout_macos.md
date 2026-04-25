# macOS QQ video send timeout

## Symptom

On a new migrated Mac, parser can download and merge Bilibili video successfully:

- `ffmpeg` merge succeeds
- output file exists
- AstrBot send then fails with `retcode=1200`
- error mentions `NodeIKernelMsgService/sendMsg`

Typical log shape:

```text
Merged BV1ZHAmzWEiB-1.mp4
发送解析结果失败： ... retcode=1200 ...
segments=[{'type': 'Video', 'media': 'file:////.../cache/BV1ZHAmzWEiB-1.mp4'}]
```

## Likely cause

This Mac is using a sandboxed QQ build:

- app: `QQ-NapCat-6.9.93-exp.app`
- container: `~/Library/Containers/com.tencent.qq.humiao.exp6993`

The merged MP4 itself is fine. `ffprobe` shows normal streams:

- video: `h264`
- audio: `aac`

But parser cache is stored under:

```text
~/Git/AstrBot/data/plugin_data/astrbot_plugin_parser/cache
```

That path is outside locations the sandboxed QQ build can freely read. Result: QQ video send helper cannot open the local file and `sendMsg` times out.

## Workaround

Put parser cache under `~/Downloads`, then symlink the original cache path to it.

```bash
mkdir -p ~/Downloads/astrbot_parser_cache
rsync -a ~/Git/AstrBot/data/plugin_data/astrbot_plugin_parser/cache/ ~/Downloads/astrbot_parser_cache/
mv ~/Git/AstrBot/data/plugin_data/astrbot_plugin_parser/cache ~/Git/AstrBot/data/plugin_data/astrbot_plugin_parser/cache.bak
ln -s ~/Downloads/astrbot_parser_cache ~/Git/AstrBot/data/plugin_data/astrbot_plugin_parser/cache
```

Then restart AstrBot and QQ/NapCat.

## Quick verification

1. Confirm parser still writes video into the cache symlink path.
2. Confirm actual file lands in `~/Downloads/astrbot_parser_cache/`.
3. Retry sending the same Bilibili link.
4. If still failing, try manual QQ send of that exact MP4 from `~/Downloads`.

If manual send also fails, issue is QQ/NapCat build specific, not parser merge logic.
