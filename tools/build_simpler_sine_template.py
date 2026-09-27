"""Regenerate rpp_to_als/templates/simpler_sine_device.xml -- the plain
sine Simpler that stands in for REAPER's ReaSynth (PLAN.md "ReaSynth ->
Simpler sine").

Source: Ableton's own Core Library preset `Simpler/Effects/Supersine.adv`
(a real Simpler playing `Sine Saw Square Sine.aif`, looped). Its sample
reference is kept VERBATIM: RelativePathType 5 = relative to the Core
Library, the same form every factory demo .als uses for its Core Library
samples, so it resolves on any Live Suite install. Only the sound
shaping is stripped back to a bare sine: filter and LFO off, no LFO
pan/pitch amounts, no voice spread, a plain organ-like envelope, -6 dB.
`LastPresetRef` is swapped for the empty `AbletonDefaultPresetRef` real
saved sets carry (from the Patience demo), so the device doesn't claim
to be the Supersine preset. Target/pointee ids are numbered -- see below.

Run: python tools/build_simpler_sine_template.py
"""

import gzip
import os
import re
import xml.etree.ElementTree as ET

CORE = r"C:\ProgramData\Ableton\Live 12 Suite\Resources\Core Library"
SUPERSINE = os.path.join(CORE, r"Devices\Instruments\Simpler\Effects\Supersine.adv")
PATIENCE = os.path.join(CORE, r"Lessons\Demo Songs\Chuck Sutton - Patience (Live 12 Suite Demo).als")
OUT = os.path.join(os.path.dirname(__file__), "..", "rpp_to_als", "templates", "simpler_sine_device.xml")

SETTINGS = {
    "Filter/IsOn/Manual": "false",
    "Lfo/IsOn/Manual": "false",
    "VolumeAndPan/PanoramaLfoAmount/Manual": "0",
    "Pitch/PitchLfoAmount/Manual": "0",
    "Globals/SpreadAmount": "0",
    "VolumeAndPan/Volume/Manual": "-6",
    "VolumeAndPan/Envelope/AttackTime/Manual": "1",
    "VolumeAndPan/Envelope/DecayTime/Manual": "600",
    "VolumeAndPan/Envelope/SustainLevel/Manual": "1",
    "VolumeAndPan/Envelope/ReleaseTime/Manual": "50",
    "UserName": "Sine (ReaSynth stand-in)",
}


def main() -> None:
    simpler = ET.parse(gzip.open(SUPERSINE)).getroot().find("OriginalSimpler")
    for path, value in SETTINGS.items():
        el = simpler.find(path)
        if el is None:
            raise SystemExit(f"Supersine.adv has no {path} -- preset format changed?")
        el.set("Value", value)

    patience = gzip.open(PATIENCE).read().decode("utf-8")
    m = re.search(r"<LastPresetRef>\s*<Value>\s*<AbletonDefaultPresetRef .*?</LastPresetRef>", patience, re.S)
    default_ref = ET.fromstring(m.group(0))
    old = simpler.find("LastPresetRef")
    idx = list(simpler).index(old)
    simpler.remove(old)
    simpler.insert(idx, default_ref)

    # A preset file leaves every AutomationTarget/ModulationTarget/Pointee
    # at Id 0. That's fine inside a Drum Rack pad, but a device placed
    # directly on a track needs them nonzero and unique -- the real
    # top-level Simpler in the Patience demo has all of them numbered,
    # and loading them as 0 failed with "Invalid Pointee Id"
    # (2026-09-27). Number them from 100, clear of the few small nonzero
    # ids the preset already carries (WarpMarker 1, preset ref, FileRef).
    next_id = 100
    for el in simpler.iter():
        if el.tag in ("AutomationTarget", "ModulationTarget", "Pointee"):
            el.set("Id", str(next_id))
            next_id += 1

    simpler.set("Id", "0")  # als_builder._embed_device forces a unique root id
    xml = ET.tostring(simpler, encoding="unicode")
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(xml)
    print(f"wrote {os.path.abspath(OUT)} ({len(xml)} chars)")


if __name__ == "__main__":
    main()
