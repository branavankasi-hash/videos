# videos

AI documentary video pipeline (Veo 3.1, Seedance 2.0, Kling 3.0, ElevenLabs).

- `scenes/` – scene definitions (prompt, duration, narration)
- `pipeline/compare_models.py` – renders one scene on every model
- `.github/workflows/compare-models.yml` – run from the Actions tab; finished MP4s are published under **Releases**

API keys live only in repository secrets: `GEMINI_API_KEY`, `ATLAS_API_KEY`, `ELEVENLABS_API_KEY`.
