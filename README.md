# yt-dlp-ncurses-handler

An interactive ncurses terminal frontend for [yt-dlp](https://github.com/yt-dlp/yt-dlp).

## Features

| Feature | Key |
|---|---|
| Firefox user-agent spoofing | `U` |
| Quality selection (4K → audio-only) | `Q` |
| Speed limit (unlimited → 100 KB/s) | `S` |
| Delay between batch downloads | `D` |
| Auto-affix source site to filename | `A` |
| Custom output filename | `O` |
| Output directory | `P` |
| Single URL input & download | `I` then `Enter` |
| Point to existing URL list file | `L` |
| Interactive list editor (create/edit) | `N` |
| Start batch download | `B` |
| Stop download | `X` |
| Clear log | `R` |
| Help screen | `?` |
| Quit | `ESC` or `Ctrl-Q` |

Live download speed and progress percentage are shown in the Download panel
and streamed into the log in real time.

## Requirements

```
pip install yt-dlp
```

Python 3.10+ (uses `curses`, stdlib only — no extra packages needed).

## Usage

```bash
python3 ytdlp_tui.py
# or after chmod +x:
./ytdlp_tui.py
```

Terminal must be at least **80×24**. Larger = more log lines visible.

## Batch list file format

Plain text, one URL per line. Lines starting with `#` are ignored.

```
# My video list
https://www.youtube.com/watch?v=...
https://vimeo.com/...
```

The interactive editor (`N`) creates/edits this file inside the TUI.
