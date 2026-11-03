# Comfy Router video apps

Two small local web apps that generate video through [Comfy Router](https://docs.comfy.org/development/comfy-router/quickstart) with the Python `comfy-sdk`:

| App | Folder | Port | What it does |
|---|---|---|---|
| **Motion Studio** | `./` (`app.py`, `static/`) | 8787 | Text-to-video: write a prompt, get one clip |
| **Verse to Film** | `poem-video/` | 8788 | Paste a short poem: each line becomes its own clip with the line on screen, then the clips are joined into one film |

Both use **LTX 2.5 Pro** (`ltx/ltx-2-5-pro`, [model page](https://docs.comfy.org/development/comfy-router/models/ltx/ltx-2-5-pro/code.md)) as the video model.

## Requirements

- Python **3.10+** (tested on 3.12)
- A Comfy workspace API key with credits ([platform.comfy.org](https://platform.comfy.org), API keys page). **Each generated clip is billed** to that workspace, and a poem costs one clip per line.
- macOS or Linux. ffmpeg comes bundled through the `imageio-ffmpeg` package, so there's nothing extra to install.

## Setup

```bash
git clone <this repo> && cd <repo>

python3 -m venv .venv            # needs Python 3.10+
.venv/bin/pip install -r requirements.txt

cp .env.example .env             # then edit .env: COMFY_API_KEY=<your key>
```

If your system Python is older than 3.10, [uv](https://docs.astral.sh/uv/) can provide one:
`uv venv -p 3.12 .venv && uv pip install -p .venv -r requirements.txt`.

`.env` must be **plain text**, with one line `COMFY_API_KEY=...`. Don't save it from TextEdit as `.rtf`. It is loaded with `load_dotenv(override=True)`, so the value in `.env` wins over any `COMFY_API_KEY` already in your shell. The key only lives on the server and is never sent to the browser. `.env` is git-ignored.

## Run

```bash
# Motion Studio → http://localhost:8787
.venv/bin/uvicorn app:app --port 8787

# Verse to Film → http://localhost:8788   (reads the same ../.env and uses the same .venv)
cd poem-video && ../.venv/bin/uvicorn app:app --port 8788
```

## Try it

**Motion Studio:** enter a prompt and pick the resolution (720p or 1080p, landscape or portrait), duration (2–20 s), frame rate and audio on or off. Click **Generate**. The card moves through QUEUED, RUNNING and FINISHED, usually in about a minute for a 5-second 720p clip. Longer or higher-resolution clips take longer, and LTX may reject some long, high-frame-rate combinations. If it does, the error is shown on the card. Then the video plays inline, and **Download MP4** saves it.

**Verse to Film:** paste up to 8 lines and set a shared visual style, the format (16:9 or 9:16) and the seconds per line (3, 4, 5, 6 or 8). Click **Make film**. All lines are submitted at once, so the whole poem takes about as long as one clip. Each line's card shows its own status. When the last clip finishes, the full film appears above the clips, and each clip and the film can be downloaded.

## How it works

```
browser ──POST──▶ FastAPI ──models.submit()──▶ Comfy Router  (returns request_id immediately)
browser ──poll──▶ FastAPI ──handle(model, request_id).status()   one poll per browser poll
                          └─ when COMPLETED: check error_type → handle.get() → download MP4 to outputs/
```

- **Queued, not synchronous.** Router cuts synchronous runs off at 10 minutes, so the apps use `models.submit()`. They save the model ID and `request_id` to `jobs.json` / `poems.json` and rebuild the handle on every poll with `models.handle(model, request_id)`. Unfinished jobs carry on across page reloads and server restarts.
- **Status.** The page shows the real queue states (`IN_QUEUE`, `IN_PROGRESS`, `COMPLETED`) plus the queue position when Router reports one. There is no fake percentage.
- **Failures.** A `COMPLETED` request can still carry an `error_type` (Router has no separate failed status), so the app checks it first. It also checks LTX's own job `status` and `error.type` / `error.message`. Either one is shown on the card.
- **Out of credits.** A `402 insufficient_credits` shows a message to add workspace credits and is not retried.
- **Downloads.** Router keeps results for 24 hours and the provider's video URLs can expire, so the server downloads each MP4 to `outputs/` as soon as it's ready.
- **Poem text overlay (Verse to Film).** The model is asked *not* to draw text. The app renders each line itself with Pillow (Georgia Italic on macOS, the default font elsewhere) and burns it in with ffmpeg at a fixed size per aspect ratio. Then it joins the finished clips with ffmpeg's concat. A failed line is left out of the film.

## Workflow files

These apps don't use a ComfyUI node graph. All generation goes through Comfy Router's hosted models. Each app's pipeline is defined in `workflows/`, and the server reads its settings from that file at startup. **What's in the JSON is what runs.**

| File | Defines |
|---|---|
| [`workflows/motion-studio.json`](workflows/motion-studio.json) | Router model, input fields with their defaults, allowed values and UI labels (the page builds its controls from these), where to find the video and errors in the model's output, and the steps: submit, poll, check, collect, download |
| [`workflows/verse-to-film.json`](workflows/verse-to-film.json) | Router model, line and duration limits, the per-line **prompt template**, the mapping from format to LTX `resolution`, fixed clip inputs (`fps: 24`, `generate_audio: false`), caption overlay settings (font, size, position, scrim, output resolution, fps), and the steps: split, submit, poll, check, collect, download, caption, burn in, join |

To change the model, the defaults, the prompt wording or the caption style, edit the JSON and restart the server. If you change the model, check its input fields on the [models page](https://docs.comfy.org/development/comfy-router/models) first (or `GET /v2/models/{provider}/{model}/openapi.json`), then update `inputs` and `output` to match. Different models take different inputs and put the video URL in different places.

The `steps` arrays describe the pipeline for people reading the file. The step order itself is in each `app.py`.

## Files

```
app.py                         Motion Studio server
static/index.html              Motion Studio UI
poem-video/app.py              Verse to Film server
poem-video/static/index.html   Verse to Film UI
workflows/motion-studio.json   Motion Studio pipeline: model, inputs, steps
workflows/verse-to-film.json   Verse to Film pipeline: model, prompt template, overlay, steps
requirements.txt               pinned deps for both apps
.env.example                   copy to .env and add your key
LICENSE                        MIT
```

Generated at runtime (git-ignored): `outputs/`, `poem-video/outputs/`, `jobs.json`, `poem-video/poems.json`.

## Notes

- Each poem line is prompted on its own, so the model reads lines literally (for example, "It sits looking" may not show the fog). The shared style field keeps the look consistent from clip to clip.
- These apps have **no login**. They're meant to run on localhost. Add authentication before exposing them anywhere, or anyone with the URL can spend your credits.

## License

[MIT](LICENSE) © 2026 Yiqing Zhao.

The code in this repo is MIT. Dependencies keep their own licenses. In particular, `imageio-ffmpeg` downloads an ffmpeg binary, which is LGPL/GPL, when you install it. This repo doesn't include ffmpeg itself. Generated videos are subject to the terms of Comfy Router and the model provider.
