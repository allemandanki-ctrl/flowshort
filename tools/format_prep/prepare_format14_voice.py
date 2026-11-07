"""
prepare_format14_voice.py
Prints the suspect's lines for a generated format14 script and creates the
folder where you drop your own audio for each one -- e.g. after generating
each line yourself (Fish Audio's generic non-clone voices, or anything
else), save it as turn_<i>.mp3 (or .wav) in the printed folder. Any missing
line just falls back to the auto-selected TTS voice, so you can fill in
as many or as few lines as you want.

Usage:
  python tools/format_prep/prepare_format14_voice.py output/scripts/<timestamp>_format14_<slug>.json
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from modules.assembler.interrogation import voice_input_dir, build_turns

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python tools/format_prep/prepare_format14_voice.py <script.json>")
        sys.exit(1)

    script = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    turns = build_turns(script)

    suspect_name = script.get("suspect_name") or script["subject"].split("/")[0]
    folder = voice_input_dir(script["subject"])
    folder.mkdir(parents=True, exist_ok=True)

    print(f"Suspect: {suspect_name}")
    print(f"Drop files here: {folder}\n")
    for i, (speaker, text) in enumerate(turns):
        if speaker != "suspect":
            continue
        print(f"  turn_{i}.mp3 (or .wav)  <-  \"{text}\"")

    print("\nAny turn left without a file uses the auto-selected TTS voice instead.")
