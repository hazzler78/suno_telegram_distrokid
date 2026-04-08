## Suno → Telegram → Cover AI → DistroKid (Python)

### Prereqs
- Python 3.11+
- PowerShell on Windows
- (Optional) Docker Desktop

### Setup (Local)
1. Create and populate `.env` from `.env.example`.
2. Create a virtual environment and install deps:
   
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   python -m playwright install --with-deps chromium
   ```

3. Run the bot:
   
   ```powershell
   $env:PYTHONUNBUFFERED=1
   python main.py
   ```

### Setup (Docker)
```powershell
docker build -t suno-telegram-distrokid .
docker run --env-file .env -p 8080:8080 -v ${PWD}\work:/app/work suno-telegram-distrokid
```

### Main Features
- Accepts Suno links (`suno.com` and `suno.ai`) and direct audio uploads from Telegram.
- Downloader tries UUID/CDN first (`cdn1.suno.ai/{uuid}.mp3` then `.wav`), then HTML parsing, then Playwright browser fallback.
- Metadata pipeline merges Suno page metadata + audio tags and keeps title, artist, genre/style, lyrics, prompt, source URL, and song UUID.
- Cover generation outputs exact `3000x3000` JPG suitable for DistroKid.
- Creates a manual-first package per track:
  - folder in `work/output/{song_id}_{title}/`
  - `audio.mp3`/`audio.wav`, `cover.jpg`, `metadata.json`
  - zip: `work/output/ready_for_distrokid_{song_id}.zip`
- Optional DistroKid auto-upload with Playwright remains available.
- SQLite progress tracking in `work/songs.db`.

### Telegram Commands
- `/start` - show quick help
- `/history` - show latest tracked songs
- `/status <song_id_or_partial_id>` - detailed status and artifact paths
- `/notes <song_id_or_partial_id> <text>` - append notes to a tracked song

### Suno Cookies / Login
- For public songs, CDN download usually works without auth.
- For private/library songs, set credentials in `.env`:
  - `SUNO_EMAIL`, `SUNO_PASSWORD`, optionally `DISCORD_EMAIL`, `DISCORD_PASSWORD`
- Playwright cookies are saved to `work/cookies/suno_cookies.json` and reused.

### Notes
- Image generation defaults to Grok/xAI:
  - set `IMAGE_PROVIDER=grok`
  - set `GROK_API_KEY=...`
  - optional: `GROK_BASE_URL=https://api.x.ai/v1`
  - optional: `IMAGE_MODEL=grok-2-image`
- You can still use OpenAI by setting:
  - `IMAGE_PROVIDER=openai`
  - `OPENAI_API_KEY=...`
  - `IMAGE_MODEL=gpt-image-1`
- DistroKid UI selectors can change; adjust `distrokid/uploader.py` if auto-upload starts failing.
- Manual package zip is the primary reliable output path for daily use.
