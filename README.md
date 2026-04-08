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

### How it works
- Send a Suno link or audio file to the bot.
- The bot downloads audio, extracts/guesses metadata, generates a 3000x3000 cover, and automates DistroKid upload.

### Notes
- Suno link direct-audio handling is supported; complex page scraping is a TODO.
- DistroKid selectors may change; adjust in `distrokid/uploader.py` as needed.
- OpenAI image generation requires `OPENAI_API_KEY`. Without it, a placeholder cover is produced.
