# Comfy Router video apps

Two small local web apps that generate video through [Comfy Router](https://docs.comfy.org/development/comfy-router/quickstart) with the Python `comfy-sdk`:

| App | Folder | Port | What it does |
|---|---|---|---|
| **Motion Studio** | `./` (`app.py`, `static/`) | 8787 | Text-to-video: write a prompt, get one clip |
| **Verse to Film** | `poem-video/` | 8788 | Paste a short poem: each line becomes its own clip, timed to how long it takes to read aloud, with the line on screen. Then the clips are joined in line order into one film |

Motion Studio uses **LTX 2.5 Pro** (`ltx/ltx-2-5-pro`, [model page](https://docs.comfy.org/development/comfy-router/models/ltx/ltx-2-5-pro/code.md)). Verse to Film lets you pick **LTX 2.5 Pro** (default), **LTX 2.5 Fast** or **Kling 3 Pro** for each film.

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

**Verse to Film** has settings at two levels:

- **Film:** the visual style (applied to every line), the **model** and the **aspect ratio**. The model dropdown lists the models that have an adapter in `workflows/verse-to-film.json` *and* appear in your workspace's live Router catalog (`models.list()`). The aspect choices follow the model: LTX offers 16:9 and 9:16, and Kling adds 1:1.
- **Line:** a **clip length** for each line. As you type the poem, each line gets a row with a suggested length based on how long it takes to read aloud. That's about 2.3 words a second, plus short pauses for commas and full stops and a breath at the end, rounded up to 0.5 s with a 2 s minimum. Edit any length (in 0.5 s steps) or **reset** it to the suggestion. Each row also shows what will be generated. For example, *"generates 4 s → trimmed to 3.5 s"*.

Click **Make film**. All lines (up to 8) are submitted at once, so the whole poem takes about as long as one clip. Each line's card shows its own status. When **every** line has finished, the clips are joined **in line order** into one film, with an inline player and a **Download full film (MP4)** link. The film's length is the sum of the line lengths. If any line fails, the film waits: the failed card shows its `error_type` and a **Retry this line** button that regenerates only that line.

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
- **Poem text overlay (Verse to Film).** The model is asked *not* to draw text. The app renders each line itself with Pillow (Georgia Italic on macOS, the default font elsewhere) and burns it in with ffmpeg at a fixed size per aspect ratio. In the same ffmpeg pass, it trims each clip to the exact length you set. If a model came back a few frames short, it holds the last frame to fill the gap.
- **Fixed durations: generate the next longer one, then trim.** LTX only accepts certain lengths (2, 3, 4, 5, 6, 8, 10 … 20 s), so each line is generated at the shortest allowed length that is at least as long as the length you set, then trimmed. Kling takes any whole number of seconds from 3 to 15, so it rounds up to the next whole second, with a 3-second minimum. A length above the model's maximum is capped at that maximum, and the row says so. **You're billed for the generated length**, so a 3.5 s line on LTX is billed as 4 s. The total under the line list shows both.
- **Joining.** Only when every line is done does the app join the clips in line order with ffmpeg's concat, re-encoding them so the timing is exact.

## Workflow files

These apps don't use a ComfyUI node graph. All generation goes through Comfy Router's hosted models. Each app's pipeline is defined in `workflows/`, and the server reads its settings from that file at startup. **What's in the JSON is what runs.**

| File | Defines |
|---|---|
| [`workflows/motion-studio.json`](workflows/motion-studio.json) | Router model, input fields with their defaults, allowed values and UI labels (the page builds its controls from these), where to find the video and errors in the model's output, and the steps: submit, poll, check, collect, download |
| [`workflows/verse-to-film.json`](workflows/verse-to-film.json) | Film defaults (style, default model, max lines), the **reading-pace constants** behind the suggested lengths, clip-length limits, the per-line **prompt template**, one **adapter per model** (see below), caption overlay settings (font, size, position, scrim, output sizes, fps), and the steps: split, plan, submit, poll, check, collect, download, caption, burn in and trim, retry, join |

To change the model, the defaults, the prompt wording or the caption style, edit the JSON and restart the server. If you change the model, check its input fields on the [models page](https://docs.comfy.org/development/comfy-router/models) first (or `GET /v2/models/{provider}/{model}/openapi.json`), then update `inputs` and `output` to match. Different models take different inputs and put the video URL in different places.

**Model adapters (Verse to Film).** Every video model takes a different request and returns a different result, so each model in the dropdown has an adapter under `models` that records:

- `aspect`: the request field for the shape, and the value to send for each aspect ratio. LTX takes `resolution`, for example `"16:9": "1280x720"`. Kling takes `aspect_ratio`.
- `duration`: the request field, plus either `"kind": "enum"` with the allowed `values` (LTX) or `"kind": "range"` with `min`, `max` and `step` (Kling).
- `fixed_inputs`: sent with every clip. For example, audio off, because the clips are joined.
- `output`: where the video URL, status and error sit in the model's result.

To add a model, read its schema at `GET https://api.comfy.org/v2/models/{provider}/{model}/openapi.json`, add an adapter and restart. It shows up in the dropdown if your workspace's catalog lists it.

The `steps` arrays describe the pipeline for people reading the file. The step order itself is in each `app.py`.

## Files

```
app.py                         Motion Studio server
static/index.html              Motion Studio UI
poem-video/app.py              Verse to Film server
poem-video/static/index.html   Verse to Film UI
workflows/motion-studio.json   Motion Studio pipeline: model, inputs, steps
workflows/verse-to-film.json   Verse to Film pipeline: model adapters, reading pace, prompt template, overlay, steps
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
