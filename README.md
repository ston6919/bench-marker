# Bench Marker

Bench Marker compares OpenRouter models on the tasks you actually care about.
Define a prompt, run several models against it, score the results, and use the
leaderboard to see which models perform best for each kind of work.

## Features

- Text benchmarks with reusable system instructions
- Live HTML previews
- Remotion motion graphics rendered to MP4
- Square HTML graphics rendered to PNG
- Strudel compositions rendered to MP3
- Incremental SVG and pen-command drawing previews
- Per-run scoring, cost tracking, reruns, cancellation, and leaderboards
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
| `GET` | `/api/leaderboard` | Aggregate model scores and costs |

## License

[MIT](LICENSE)
