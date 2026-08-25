# reels-video-editor

Flask/FFmpeg service used by the unified Reels AI workflow.

## Endpoints

- `GET /health` — service health.
- `GET /capabilities` — API contract and upload limits.
- `POST /analyze` — video metadata, silence intervals, speech segments, and safe suggested cuts.
- `POST /extract-audio` — extracts a compact 16 kHz MP3 for speech transcription.
- `POST /edit` — applies `CUT`, `ZOOM`, `TEXT`, and `SUBTITLE` actions and returns an MP4.

Both POST endpoints use `multipart/form-data`. The video field is named `video`.

### Analyze

```bash
curl -X POST https://YOUR-SERVICE.onrender.com/analyze \
  -F 'video=@source.mp4'
```

### Edit

```bash
curl -X POST https://YOUR-SERVICE.onrender.com/edit \
  -F 'video=@source.mp4' \
  -F 'output_name=reel_43.mp4' \
  -F 'actions=[{"action":"CUT","start":0,"end":0.8},{"action":"ZOOM","start":1,"end":2,"scale":1.1},{"action":"SUBTITLE","start":1,"end":3,"text":"Проверим этот симптом","highlight":"симптом"}]' \
  --output reel_43.mp4
```

## Optional API protection

Set `VIDEO_EDITOR_API_KEY` in Render. Then send the same value in the `X-API-Key` header from n8n. If the variable isn't set, the API remains backwards compatible and accepts editing calls without the header.

Other environment variables:

- `MAX_UPLOAD_MB` — maximum request size, default `250`.
- `PORT` — service port, default `10000`.

## Unified n8n workflow

Import `unified-reels-ai.json` into n8n. The workflow keeps the existing scenario generation branch and adds a Telegram video branch:

1. Send a source video to the Reels AI bot with the Google Sheets `ID` in the caption, for example `43` or `reels 43`.
2. The workflow validates that the scenario is `Готово` and its decision is `Принять`.
3. It analyzes the video, transcribes speech, creates safe editing actions, calls `/edit`, and sends the edited MP4 back to the same Telegram chat.

The standard Telegram Bot API can download files up to 20 MB. Compress larger source files before sending them to the bot.

## Local checks

```bash
python -m unittest -v
```
