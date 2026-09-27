"""Per-track FX inventory for both export directions: which FX/devices
made it across and which didn't, so a migration can be finished by hand
(PLAN.md "FX report"). Printed by the CLI, shown as a list in the GUI."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FxEntry:
    track: str
    fx: str
    converted: bool
    note: str = ""
    count: int = 1  # identical instances on the same track, e.g. a 50-pad RS5k kit

    @property
    def label(self) -> str:
        return f"{self.fx} (x{self.count})" if self.count > 1 else self.fx


def group(entries: list[FxEntry]) -> list[FxEntry]:
    """Merge identical entries (same track, FX, status, note) into one
    with a count, keeping first-appearance order."""
    out: list[FxEntry] = []
    index: dict[tuple, FxEntry] = {}
    for e in entries:
        key = (e.track, e.fx, e.converted, e.note)
        if key in index:
            index[key].count += e.count
        else:
            index[key] = FxEntry(e.track, e.fx, e.converted, e.note, e.count)
            out.append(index[key])
    return out


def format_report(entries: list[FxEntry]) -> list[str]:
    if not entries:
        return ["FX report: no FX/devices found."]
    converted = sum(e.count for e in entries if e.converted)
    missing = sum(e.count for e in entries if not e.converted)
    lines = [f"FX report: {converted} converted, {missing} not converted"]
    for e in entries:
        status = "converted" if e.converted else "NOT converted"
        note = f" -- {e.note}" if e.note else ""
        lines.append(f"  [{status}] {e.track}: {e.label}{note}")
    return lines
