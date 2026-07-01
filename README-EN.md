# cooper-autoclip-03

Cooper AutoClip 03 is an AI-assisted video clipping tool for personal publishing workflows. It imports videos from YouTube, Bilibili, or local files, analyzes subtitles, selects useful moments, generates titles, and exports reusable clips.

## Features

- YouTube and Bilibili URL import.
- Local video import with optional SRT subtitles.
- Configurable LLM providers through the settings page.
- Manual clip controls for target count, minimum duration, and maximum duration.
- Long candidates are trimmed to the configured maximum duration instead of producing empty projects.
- Completed projects can be deleted from the project list.

## Quick Start

```bash
git clone https://github.com/milanyangqi/cooper-autoclip-03.git
cd cooper-autoclip-03
./start_autoclip.sh
```

Open:

```text
http://localhost:3000
```

Stop services:

```bash
./stop_autoclip.sh
```

Check status:

```bash
./status_autoclip.sh
```

## Data

Generated data, uploaded videos, databases, and exported clips are stored under `data/` and are ignored by Git.

## License

Released under the MIT License. Keep the included `LICENSE` file with redistributions.
