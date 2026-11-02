import io
import json
import queue
import re
import subprocess
import threading
import time
import urllib.request
import uuid
import wave

import numpy as np
import rumps
import sounddevice as sd
import webrtcvad
from openwakeword.model import Model
from quickmachotkey import mask, quickHotKey
from quickmachotkey.constants import cmdKey, controlKey, kVK_ANSI_J, optionKey

import actions
import mac_tools
from brain import Brain

ROOT = actions.ROOT
MODELS = ROOT / "models"
SETTINGS = actions.STATE / "settings.json"
WHISPER_PORT = 8178
RATE = 16000
CHUNK = 1280
WAKE_THRESHOLD = 0.5
WHISPER_HINT = actions.CONFIG.get("whisper_hint", "Max, tmux, Claude.")
VOICES = ["am_michael", "af_heart", "am_fenrir", "am_puck", "bm_george", "bm_fable", "bm_lewis", "af_bella"]
JUNK = {"", "you", "thank you.", "thanks for watching!", "[blank_audio]", "(silence)", "[ silence ]", "bye."}
YES = re.compile(r"^\W*(yes|yeah|yep|yup|sure|go|go ahead|do it|ok|okay|confirm|please|sige|oo|g)\b", re.I)
NO = re.compile(r"^\W*(no|nope|don'?t|cancel|stop|wait|hold on|never ?mind|wag|huwag|hindi)\b", re.I)


def load_settings():
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return {}


def save_settings(s):
    SETTINGS.write_text(json.dumps(s))


def for_speech(text):
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"[*_`#>|]", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    text = re.sub(r"₱\s?([\d,.]+)", r"\1 pesos", text)
    text = text.replace("~", "about ").replace("—", ", ").replace("&", " and ")
    return re.sub(r"\s+", " ", text).strip()


def sentences(text):
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p for p in parts if p.strip()]


def is_noise(text, follow_up):
    t = text.lower().strip()
    if t in JUNK or len(re.sub(r"[^a-z]", "", t)) < 4:
        return not (follow_up and (YES.search(t) or NO.search(t)))
    return follow_up and len(t.split()) < 2 and not (YES.search(t) or NO.search(t))


def chime(name):
    subprocess.Popen(["afplay", f"/System/Library/Sounds/{name}.aiff"])


class Voice:
    def __init__(self, voice):
        from kokoro_onnx import Kokoro
        self.tts = Kokoro(str(MODELS / "kokoro-v1.0.fp16.onnx"), str(MODELS / "voices-v1.0.bin"))
        self.voice = voice
        self.q = queue.Queue()
        self.busy = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def say(self, text):
        for s in sentences(for_speech(text)):
            self.busy.set()
            self.q.put(s)

    def _run(self):
        while True:
            s = self.q.get()
            if s is None:
                return
            try:
                audio, sr = self.tts.create(s, voice=self.voice, speed=1.05, lang="en-us")
                sd.play(audio, sr)
                sd.wait()
            except Exception as e:
                print("tts error", e)
            if self.q.empty():
                self.busy.clear()

    def shut_up(self):
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        sd.stop()
        self.busy.clear()

    def wait(self):
        while self.busy.is_set():
            time.sleep(0.05)

    def close(self):
        self.shut_up()
        self.q.put(None)


class Ears:
    def __init__(self):
        self.whisper = subprocess.Popen(
            ["whisper-server", "-m", str(MODELS / "ggml-large-v3-turbo-q5_0.bin"), "--host", "127.0.0.1",
             "--port", str(WHISPER_PORT), "-l", "en", "-nt", "-t", "4", "--prompt", WHISPER_HINT],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wake = Model(wakeword_models=[str(MODELS / "wake_word.onnx")], inference_framework="onnx",
                          melspec_model_path=str(MODELS / "melspectrogram.onnx"),
                          embedding_model_path=str(MODELS / "embedding_model.onnx"))
        self.vad = webrtcvad.Vad(2)
        self.q = queue.Queue()
        self.chunks = 0
        self.stream = None
        self.open_mic()

    def _on_audio(self, data, frames, t, status):
        self.chunks += 1
        self.q.put(data[:, 0].copy())

    def open_mic(self):
        if self.stream:
            try:
                self.stream.abort()
                self.stream.close()
            except Exception:
                pass
        sd._terminate()
        sd._initialize()
        self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=CHUNK,
                                     callback=self._on_audio)
        self.stream.start()
        return sd.query_devices(kind="input")["name"]

    def flush(self):
        while not self.q.empty():
            self.q.get_nowait()
        self.wake.reset()

    def record(self, start_timeout=5.0, silence=0.9, max_len=25.0):
        frames, buf = [], np.zeros(0, dtype=np.int16)
        heard = False
        quiet = 0.0
        t0 = time.time()
        while True:
            try:
                buf = np.concatenate([buf, self.q.get(timeout=0.5)])
            except queue.Empty:
                pass
            while len(buf) >= 480:
                frame, buf = buf[:480], buf[480:]
                speech = self.vad.is_speech(frame.tobytes(), RATE)
                if speech:
                    heard, quiet = True, 0.0
                elif heard:
                    quiet += 0.03
                if heard or speech:
                    frames.append(frame)
            elapsed = time.time() - t0
            if not heard and elapsed > start_timeout:
                return None
            if heard and (quiet >= silence or elapsed > max_len):
                return np.concatenate(frames)

    def transcribe(self, audio):
        wav = io.BytesIO()
        with wave.open(wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(audio.tobytes())
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\njson\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
                f"Content-Type: audio/wav\r\n\r\n").encode() + wav.getvalue() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(f"http://127.0.0.1:{WHISPER_PORT}/inference", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        for _ in range(40):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.loads(r.read()).get("text", "").strip()
            except (ConnectionError, urllib.error.URLError):
                time.sleep(0.5)
        return ""

    def close(self):
        self.stream.stop()
        self.stream.close()
        self.whisper.terminate()


class Max(rumps.App):
    def __init__(self):
        super().__init__("Max", title="M ◦", quit_button=None)
        self.settings = load_settings()
        self.brain = Brain()
        self.voice = self.ears = None
        self.state = "off"
        self.stop = threading.Event()
        self.talk_now = threading.Event()
        self.listen_item = rumps.MenuItem("Listening", callback=self.toggle)
        voice_menu = rumps.MenuItem("Voice")
        for v in VOICES:
            voice_menu.add(rumps.MenuItem(v, callback=self.pick_voice))
        self.menu = [self.listen_item, rumps.MenuItem("Talk now", callback=self.on_talk),
                     rumps.MenuItem("Stop talking", callback=self.on_shut_up),
                     rumps.MenuItem("New conversation", callback=self.on_new), None, voice_menu,
                     rumps.MenuItem("Open log", callback=lambda _: subprocess.Popen(["open", "-t", str(actions.STATE / "log.jsonl")])),
                     None, rumps.MenuItem("Quit Max", callback=self.on_quit)]
        self.mark_voice()
        rumps.Timer(self.refresh, 0.25).start()
        rumps.Timer(lambda _: threading.Thread(target=self.brain.maybe_rollover, daemon=True).start(), 300).start()
        rumps.Timer(self.heartbeat, 60).start()
        rumps.Timer(self.check_alarms, 10).start()

        @quickHotKey(virtualKey=kVK_ANSI_J, modifierMask=mask(cmdKey, controlKey, optionKey))
        def hotkey():
            self.toggle(None)
        self.hotkey = hotkey
        self.toggle(None)

    def refresh(self, _):
        icons = {"off": "M ◦", "idle": "M ●", "listening": "M ◉", "thinking": "M …", "speaking": "M ♪", "loading": "M ⋯"}
        self.title = icons.get(self.state, "J")
        self.listen_item.state = self.state != "off"

    def check_alarms(self, _):
        for a in mac_tools.due_alarms():
            threading.Thread(target=self.ring, args=(a["label"],), daemon=True).start()

    def ring(self, label):
        for _ in range(3):
            subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"])
        if self.voice:
            self.voice.say(f"Alarm. {label}.")
        else:
            rumps.notification("Max", "Alarm", label)

    def heartbeat(self, _):
        if self.ears:
            self.brain._log({"heartbeat": self.state, "mic_chunks_per_min": self.ears.chunks})
            self.ears.chunks = 0

    def mark_voice(self):
        cur = self.settings.get("voice", VOICES[0])
        for v in VOICES:
            self.menu["Voice"][v].state = v == cur

    def pick_voice(self, item):
        self.settings["voice"] = item.title
        save_settings(self.settings)
        self.mark_voice()
        if self.voice:
            self.voice.voice = item.title
            self.voice.say("This is how I sound now.")

    def toggle(self, _):
        if self.state == "off":
            chime("Submarine")
            self.stop.clear()
            self.state = "loading"
            threading.Thread(target=self.loop, daemon=True).start()
        elif not self.stop.is_set():
            chime("Bottle")
            self.stop.set()
            self.brain.stop()
            if self.voice:
                self.voice.shut_up()

    def on_talk(self, _):
        self.talk_now.set()

    def on_shut_up(self, _):
        if self.voice:
            self.voice.shut_up()

    def on_new(self, _):
        actions.clear_pending()
        if self.voice:
            self.voice.say("Saving today's notes, then starting fresh.")
        threading.Thread(target=self.brain.reset, daemon=True).start()

    def on_quit(self, _):
        self.stop.set()
        self.brain.close()
        if self.ears:
            self.ears.whisper.terminate()
        rumps.quit_application()

    def loop(self):
        try:
            threading.Thread(target=self.brain.warm, daemon=True).start()
            self.voice = Voice(self.settings.get("voice", VOICES[0]))
            self.ears = Ears()
            self.state = "idle"
            follow_up = 0.0
            while not self.stop.is_set():
                in_follow_up = bool(follow_up)
                if follow_up:
                    self.state = "listening"
                    audio = self.ears.record(start_timeout=follow_up)
                    follow_up = 0.0
                else:
                    self.state = "idle"
                    if not self.wait_for_trigger():
                        continue
                    chime("Tink")
                    self.state = "listening"
                    audio = self.ears.record()
                if audio is None:
                    continue
                self.state = "thinking"
                chime("Pop")
                t0 = time.time()
                text = self.ears.transcribe(audio)
                noise = is_noise(text, in_follow_up)
                self.brain._log({"heard": text, "audio_s": round(len(audio) / RATE, 1),
                                 "stt_s": round(time.time() - t0, 2),
                                 "mode": "follow-up" if in_follow_up else "wake", "ignored": noise})
                if noise or self.stop.is_set():
                    continue
                follow_up = self.handle(text)
                self.state = "speaking"
                self.voice.wait()
                time.sleep(0.25)
                self.ears.flush()
        except Exception as e:
            print("max loop crashed:", e)
            rumps.notification("Max", "Stopped", str(e))
        finally:
            self.brain.close()
            for part in (self.ears, self.voice):
                if part:
                    part.close()
            self.ears = self.voice = None
            self.state = "off"

    def wait_for_trigger(self):
        self.talk_now.clear()
        trigger = threading.Event()

        def watch_button():
            while not trigger.is_set() and not self.stop.is_set():
                if self.talk_now.wait(0.2):
                    trigger.set()
        threading.Thread(target=watch_button, daemon=True).start()
        last_audio = time.time()
        while not trigger.is_set() and not self.stop.is_set():
            try:
                chunk = self.ears.q.get(timeout=0.2)
                last_audio = time.time()
            except queue.Empty:
                if time.time() - last_audio > 3:
                    device = self.ears.open_mic()
                    self.brain._log({"mic": "no audio for 3s, reopened", "device": device})
                    last_audio = time.time()
                continue
            if max(self.ears.wake.predict(chunk).values()) > WAKE_THRESHOLD:
                self.ears.wake.reset()
                trigger.set()
        if self.talk_now.is_set():
            self.ears.flush()
        return trigger.is_set() and not self.stop.is_set()

    def handle(self, text):
        pending = actions.load_pending()
        if pending:
            actions.clear_pending()
            if YES.search(text):
                return self.run_action(pending)
            if NO.search(text):
                self.voice.say("Okay, I won't.")
                self.brain.last = time.time()
                return 6.0
            text = f"({actions.NAME} did not confirm the proposed action, so it was dropped.) {text}"
        return self.think(text)

    def think(self, text):
        if not actions.master_alive():
            self.voice.say("Tap your watch so I can reach the server.")
            if not actions.open_master():
                self.voice.say("I couldn't connect to the server.")
        spoke = []

        def on_text(t):
            spoke.append(t)
            self.state = "speaking"
            self.voice.say(t)

        def on_tool(name, args):
            self.state = "thinking"
            if not spoke:
                spoke.append("")
                self.voice.say("One sec.")

        self.state = "thinking"
        self.brain.ask(text, on_text, on_tool)
        return 10.0 if actions.load_pending() else 6.0

    def run_action(self, action):
        if action.get("destructive"):
            self.voice.say("Tap your watch to approve.")
        else:
            self.voice.say("On it.")
        try:
            rc, out = actions.execute(action)
        except Exception as e:
            rc, out = 1, str(e)
        status = "succeeded" if rc == 0 else f"failed with exit code {rc}"
        return self.think(f"({actions.NAME} said yes. Action '{action['summary']}' {status}. Output:\n{out}\n"
                          f"Tell {actions.NAME} the result in one or two sentences.)")


if __name__ == "__main__":
    Max().run()
