# Bench Marker

Bench Marker compares OpenRouter models on the tasks you actually care about.
Define a prompt, run several models against it, and score the results to compare
which models perform best for each kind of work.

## Features

- Text benchmarks with reusable system instructions
- Live HTML previews
- Remotion motion graphics rendered to MP4
- Square HTML graphics rendered to PNG
- Strudel compositions rendered to MP3
- Incremental SVG and pen-command drawing previews
- Per-run scoring, cost tracking, reruns, and cancellation
- Configurable per-run OpenRouter cost limit

## Requirements

- Python 3.11 or newer (the server uses only the standard library)
- Node.js 22.12 or newer and npm
- FFmpeg for MP3 output
- A Chromium-compatible browser for PNG and Strudel rendering
- An [OpenRouter](https://openrouter.ai/) API key

Text, HTML, SVG, and pen benchmarks do not require the media-rendering tools.

## Quick start

```bash
git clone https://github.com/ston6919/bench-marker.git
cd bench-marker
cp .env.example .env
```

Add your key to `.env`:

```dotenv
OPENROUTER_API_KEY=your-key-here
```

Install the two rendering templates and start the server:

```bash
npm ci --prefix remotion-template
npm ci --prefix strudel-template
python3 server.py
```

Open `http://127.0.0.1:8796/`.

The Strudel bundle is generated during `npm ci`. You can rebuild it with
`npm run build --prefix strudel-template`.

## Create your own tests

A test is one prompt or creative brief run against one or more models inside a
built-in benchmark type. You create tests from the browser; you do not need to
edit source files or write the generated HTML, SVG, Remotion, or Strudel code
yourself.

### Basic workflow

1. Open **Settings**, add your OpenRouter API key, and set a per-run cost limit.
2. Open the benchmark tab that matches the output you want, such as **Text +
   skills** or **Remotion motion graphic**.
3. Select **Create test**.
4. Enter the prompt or brief. Text tests also require a reusable skill; game
   tests ask you to choose a game and accept optional extra instructions.
5. Search the OpenRouter catalogue and select one or more models.
6. Select **Create test** again to start the run. Every selected model receives
   the same prompt and appears as a separate result in the test group.
7. Compare the rendered result and source or prompt, then optionally give each
   result a score from 0–10 and add scoring notes.

Each selected model is a separate billable OpenRouter request. **Re-run** starts
a new request for one result. **Add model to test** runs another model with the
exact prompt already saved for that test, which is useful for fair comparisons.
You can also stop an active run or permanently delete an individual result.

### Choose a test type

| Benchmark tab | What you provide | What Bench Marker produces |
| --- | --- | --- |
| **Text + skills** | A reusable instruction (skill) and a user prompt | Plain-text responses |
| **HTML page generation** | A description of a page or interface | Standalone HTML with a live preview |
| **Game maker** | Tetris, Pac-Man, or Flappy Bird, plus optional style/rule instructions | A playable browser game |
| **Remotion motion graphic** | A description of a short animation | Model-written Remotion/React code rendered to MP4 |
| **Square image graphic** | A description of a square social graphic | Model-written HTML captured as a 1080×1080 PNG |
| **Strudel song → MP3** | A description of the music, mood, instruments, and tempo | Model-written Strudel code rendered to MP3 |
| **Live SVG draw** | A description of what to illustrate | SVG that appears as complete shapes stream in |
| **Live pen drawing** | A description of what to sketch | Animated pen strokes on a square canvas |

The media types have additional local dependencies. Remotion requires its Node
template and browser; square images require Chromium; Strudel requires its Node
template, Chromium, and FFmpeg. See [Rendering setup](#rendering-setup).

### Create a Text + skills test

A skill is the reusable system instruction applied to a text test. For example,
you might create a skill named `Concise technical reviewer` with instructions
about tone, format, review criteria, and what the response must include.

1. Open **Text + skills** and select **Create test**.
2. Select **Manage skills**, then create or edit a named instruction.
3. Select that skill and enter the user prompt containing the actual material or
   question to process.
4. Select the models and start the test.

Bench Marker saves a snapshot of both the skill and user prompt with every
result, so editing the skill later does not change what an earlier test used.

### Create a Remotion or other creative test

Describe the result rather than writing implementation code. For a Remotion
test, specify details such as duration, visual hierarchy, colours, copy, motion,
and timing. For an HTML page, image, drawing, or song, describe the desired
content and style just as you would brief a designer or developer. Bench Marker
adds the technical output rules and asks each selected model to generate the
appropriate source format.

The current interface creates tests within the eight built-in benchmark types.
Adding an entirely new output type or renderer requires extending the
application code.

## Configuration

Bench Marker reads `.env` from the repository root and accepts these variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | none | OpenRouter credential |
| `HOST` | `127.0.0.1` | HTTP bind address |
| `PORT` | `8796` | HTTP port |
| `BENCH_MARKER_DATA_DIR` | `./data` | Runtime data, settings, work, and renders |
| `BENCH_MARKER_DB` | `<data-dir>/bench.db` | SQLite database override |
| `OPENROUTER_HTTP_REFERER` | local server URL | OpenRouter request attribution URL |
| `OPENROUTER_APP_TITLE` | `Bench Marker` | OpenRouter request attribution name |
| `NODE_BIN` / `NPX_BIN` | auto-detected | Node executable overrides |
| `FFMPEG_BIN` | auto-detected | FFmpeg executable override |
| `CHROME_BIN` | auto-detected | Chromium executable override |
| `REMOTION_BROWSER_EXECUTABLE` | auto-detected | Remotion browser override |

Additional render timeout and quality controls are documented in
[`.env.example`](.env.example).

An API key entered through the Settings screen is stored locally in
`data/settings.json` with owner-only permissions and takes precedence over the
environment. That file, the SQLite database, prompts, outputs, and generated
media are intentionally excluded from Git.

## Rendering setup

Remotion keeps its dependencies and downloaded browser under
`remotion-template/`. Each model response is copied into an ignored work
directory before rendering.

Strudel uses a browser bundle built from `strudel-template/src/`, then captures
audio through headless Chromium and converts the WAV file to MP3 with FFmpeg.
Set `CHROME_BIN` if a browser cannot be detected automatically.

## Running as a systemd service

The included [`bench-marker.service`](bench-marker.service) assumes:

- application files at `/opt/bench-marker`
- a system user and group named `bench-marker`
- writable runtime data at `/var/lib/bench-marker`
- optional secrets and overrides in `/etc/bench-marker.env`

Create the service account and runtime directory, then install the unit:

```bash
sudo useradd --system --home /opt/bench-marker --shell /usr/sbin/nologin bench-marker
sudo install -d -o bench-marker -g bench-marker /var/lib/bench-marker
sudo cp bench-marker.service /etc/systemd/system/bench-marker.service
sudo systemctl daemon-reload
sudo systemctl enable --now bench-marker.service
```

Create `/etc/bench-marker.env` with mode `600` when environment-based secrets
or executable overrides are needed.

For a subpath reverse proxy, strip the prefix before forwarding:

```nginx
location = /bench-marker {
    return 301 /bench-marker/;
}

location /bench-marker/ {
    proxy_pass http://127.0.0.1:8796/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

## Security and cost warning

Bench Marker has no built-in authentication and can initiate billable API
requests. Keep it bound to loopback and protect any remote deployment with an
authenticated, TLS-enabled reverse proxy. Configure the cost limit in Settings
before testing unfamiliar models. See [`SECURITY.md`](SECURITY.md) for the
responsible-disclosure and deployment guidance.

## Tests

```bash
python3 -m compileall -q db.py server.py remotion_render.py strudel_render.py html_image_render.py
python3 -m unittest discover -v
```

GitHub Actions runs the same checks for pushes and pull requests.

## API overview

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Health and masked configuration status |
| `GET/POST` | `/api/tasks` | List or create tasks |
| `PATCH/DELETE` | `/api/tasks/:id` | Update or delete a task |
| `GET/POST` | `/api/models` | List or add models |
| `GET` | `/api/openrouter/models` | Fetch the OpenRouter catalog |
| `GET/POST` | `/api/runs` | List or start benchmark runs |
| `POST` | `/api/runs/:id/score` | Score a run |
| `POST` | `/api/runs/:id/stop` | Cancel an in-progress run |
| `POST` | `/api/runs/:id/rerun` | Repeat a task/model run |
| `GET` | `/api/runs/:id/video` | Fetch a rendered MP4 |
| `GET` | `/api/runs/:id/image` | Fetch a rendered PNG |
| `GET` | `/api/runs/:id/audio` | Fetch a rendered MP3 |
| `GET/POST` | `/api/settings` | Read masked settings or update local settings |

## License

[MIT](LICENSE)
