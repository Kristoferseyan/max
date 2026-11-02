# Max

A "Hey Max" voice assistant for the Mac, because Siri AI is too weak. I need access to everything, everywhere, anywhere.

Say the wake word, talk, and it answers out loud. It can peek at a server over SSH (tmux windows, docker, logs, what your Claude Code sessions are up to), and it handles Apple Reminders, Calendar, alarms and timers on the Mac.

## How it works

openWakeWord hears the wake word, webrtcvad decides when you stopped talking, whisper.cpp's `whisper-server` turns it into text, and one long-running `claude -p` answers using the tools in `tools_server.py`. Kokoro reads the reply back to you. One Claude session lasts a day (rolls over at 4am) and writes a journal that tomorrow's session starts with, so it remembers what you told it.

## What you need

- macOS, Python 3.12, and `whisper-server` on your PATH (`brew install whisper-cpp`)
- Claude Code, logged in (`claude` on your PATH)
- A server you can `ssh` into with key auth, with tmux and docker on it
- Models in `models/`: openWakeWord v0.5.1 (`wake_word.onnx`, `melspectrogram.onnx`, `embedding_model.onnx`), Kokoro (`kokoro-v1.0.fp16.onnx`, `voices-v1.0.bin`) and whisper.cpp `ggml-large-v3-turbo-q5_0.bin`

## Setup

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt quickmachotkey pyobjc-framework-EventKit
cp config.example.json config.json
./max
```

In `config.json`, set `name` (what Max calls you), `host` (an SSH host or alias, or use `MAX_HOST`), `sessions` (your tmux session names and what runs in each), the `whisper_hint` words speech-to-text keeps getting wrong, and optionally `background_files` Claude may read for context. `MAX_MODEL` picks the Claude model (default `sonnet`).

The menu bar shows `M ●` while it waits for the wake word. ⌃⌥⌘J toggles listening. After an answer you get a few seconds to reply without saying the wake word again. Mic, Reminders and Calendar permissions go to whatever app launched it, usually your terminal. Logs are in `state/log.jsonl`. Quit from the menu, not with kill, or whisper-server sticks around (`./max` cleans that up on the next start anyway).

## Safety

Claude gets no shell. On the Mac it can only Read, Grep and Glob, and on the server it gets a short list of read-only commands. Anything that changes something (typing into a tmux window, restarting a container, running a command) goes through `propose_action`, which only writes the plan to `state/pending.json`. Max asks you, and runs it only if you say yes out loud. Taglish counts: "sige" and "oo" mean yes, "wag" means no.

Destructive or sudo actions open a fresh SSH connection instead of reusing the shared one, so if your key needs a tap or a touch to sign (Secretive, a security key), you get asked again. `blocked_sessions` and `blocked_containers` in the config hide anything you don't want it near.

## Wake word

`wake_word.onnx` is one of openWakeWord's pretrained models, still trained on a different name's sound. Retraining it to actually hear "Hey Max" needs synthetic speech data and a fine-tune; that's a separate pending task.

## License

MIT
