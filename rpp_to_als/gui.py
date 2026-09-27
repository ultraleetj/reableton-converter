"""wxPython GUI for rpp_to_als -- thin front end over cli.py's
export_project / export_rpp (no reimplemented export logic here, per
PLAN.md milestone 6). The direction follows the input file: a .rpp
exports to .als, a .als exports back to .rpp. Screen-reader accessibility rules followed throughout are
in ACCESSIBILITY.md at the repo root -- read that before touching this
file, especially before adding any new control.

Run: python -m rpp_to_als.gui
"""

from __future__ import annotations

import contextlib
import io
import os
import threading

import wx

from .cli import _LIVETEST_DIR, _LIVETEST_RPP_DIR, DEFAULT_MEDIA_SUBDIR, export_project, export_rpp
from .fx_report import FxEntry, format_report


class FxReportDialog(wx.Dialog):
    """Every FX/device found in the last export and whether it made it
    across -- the checklist for finishing a migration by hand."""

    def __init__(self, parent: wx.Frame, entries: list[FxEntry]):
        super().__init__(parent, title="FX report", size=(760, 420), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._entries = entries
        converted = sum(e.count for e in entries if e.converted)
        missing = sum(e.count for e in entries if not e.converted)
        summary = f"{converted} converted, {missing} not converted." if entries else "No FX or devices found."

        sizer = wx.BoxSizer(wx.VERTICAL)
        lbl = wx.StaticText(self, label=summary)
        self._list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL, name="FX report")
        for col, (title, width) in enumerate((("Track", 140), ("FX", 280), ("Status", 110), ("Note", 220))):
            self._list.InsertColumn(col, title, width=width)
        for row, e in enumerate(entries):
            self._list.InsertItem(row, e.track)
            self._list.SetItem(row, 1, e.label)
            self._list.SetItem(row, 2, "converted" if e.converted else "NOT converted")
            self._list.SetItem(row, 3, e.note)
        if entries:
            self._list.Select(0)
            self._list.Focus(0)
        copy_btn = wx.Button(self, label="Copy as text", name="Copy FX report as text")
        copy_btn.Bind(wx.EVT_BUTTON, self._on_copy)
        copy_btn.Enable(bool(entries))

        sizer.Add(lbl, 0, wx.ALL, 8)
        sizer.Add(self._list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(copy_btn, 0, wx.ALL, 8)
        sizer.Add(self.CreateButtonSizer(wx.OK), 0, wx.EXPAND | wx.ALL, 8)
        self.SetEscapeId(wx.ID_OK)
        self.SetSizer(sizer)

    def _on_copy(self, evt: wx.CommandEvent) -> None:
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject("\n".join(format_report(self._entries))))
            wx.TheClipboard.Close()
            self.GetParent().SetStatusText("FX report copied to the clipboard.")


class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title="ReAbleton", size=(720, 560))
        self._last_output: str | None = None
        self._last_report: list[FxEntry] | None = None
        self._exporting = False

        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)

        outer.Add(self._build_project_row(panel), 0, wx.EXPAND | wx.ALL, 8)
        outer.Add(self._build_output_row(panel), 0, wx.EXPAND | wx.ALL, 8)
        outer.Add(self._build_output_name_row(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        outer.Add(self._build_copy_samples_row(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        outer.Add(self._build_project_export_buttons(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        outer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.ALL, 8)

        self._log = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP | wx.HSCROLL,
            name="Export log",
        )
        self._log.SetFont(wx.Font(wx.FontInfo(10).Family(wx.FONTFAMILY_TELETYPE)))
        outer.Add(self._log, 1, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(outer)

        self.CreateStatusBar()
        self.SetStatusText("Pick a REAPER project (.rpp) or an Ableton Live Set (.als) to begin.")

        self._update_direction()
        self._update_button_states()

    # -- layout ------------------------------------------------------

    def _build_project_row(self, panel: wx.Panel) -> wx.Sizer:
        box = wx.BoxSizer(wx.VERTICAL)
        lbl = wx.StaticText(panel, label="Project to convert (REAPER .rpp -> Ableton, or Ableton .als -> REAPER):")
        row = wx.BoxSizer(wx.HORIZONTAL)
        self._project_ctrl = wx.TextCtrl(panel, name="Project to convert")
        self._project_ctrl.Bind(wx.EVT_TEXT, self._on_project_changed)
        browse = wx.Button(panel, label="Browse...", name="Project to convert Browse button")
        browse.Bind(wx.EVT_BUTTON, self._on_browse_project)
        row.Add(self._project_ctrl, 1, wx.EXPAND | wx.RIGHT, 4)
        row.Add(browse, 0)
        box.Add(lbl, 0, wx.BOTTOM, 2)
        box.Add(row, 0, wx.EXPAND)
        return box

    def _build_output_row(self, panel: wx.Panel) -> wx.Sizer:
        box = wx.BoxSizer(wx.VERTICAL)
        lbl = wx.StaticText(panel, label="Output folder (blank = same folder as project):")
        row = wx.BoxSizer(wx.HORIZONTAL)
        self._output_ctrl = wx.TextCtrl(panel, name="Output folder")
        browse = wx.Button(panel, label="Browse...", name="Output folder Browse button")
        browse.Bind(wx.EVT_BUTTON, self._on_browse_output)
        row.Add(self._output_ctrl, 1, wx.EXPAND | wx.RIGHT, 4)
        row.Add(browse, 0)
        box.Add(lbl, 0, wx.BOTTOM, 2)
        box.Add(row, 0, wx.EXPAND)
        return box

    def _build_output_name_row(self, panel: wx.Panel) -> wx.Sizer:
        box = wx.BoxSizer(wx.VERTICAL)
        lbl = wx.StaticText(panel, label="Output name (blank = same as project file):")
        self._output_name_ctrl = wx.TextCtrl(panel, name="Output name")
        box.Add(lbl, 0, wx.BOTTOM, 2)
        box.Add(self._output_name_ctrl, 0, wx.EXPAND)
        return box

    def _build_copy_samples_row(self, panel: wx.Panel) -> wx.Sizer:
        box = wx.BoxSizer(wx.VERTICAL)
        self._copy_samples_ctrl = wx.CheckBox(
            panel, label="Copy sample WAVs locally (portable export)", name="Copy sample WAVs locally"
        )
        self._export_standalone_adg_ctrl = wx.CheckBox(
            panel,
            label="Also export standalone .adg per Drum Rack track",
            name="Also export standalone .adg per Drum Rack track",
        )
        self._scenes_ctrl = wx.CheckBox(
            panel,
            label="Turn project markers into Session-view Scenes",
            name="Turn project markers into Session-view Scenes",
        )
        box.Add(self._copy_samples_ctrl, 0)
        box.Add(self._export_standalone_adg_ctrl, 0, wx.TOP, 4)
        box.Add(self._scenes_ctrl, 0, wx.TOP, 4)
        return box

    def _build_project_export_buttons(self, panel: wx.Panel) -> wx.Sizer:
        row = wx.BoxSizer(wx.HORIZONTAL)
        self._export_project_btn = wx.Button(panel, label="Export whole project -> .als", name="Export whole project to .als")
        self._export_project_btn.Bind(wx.EVT_BUTTON, lambda evt: self._start_export_project(test=False))
        self._quick_export_btn = wx.Button(
            panel, label="Quick export -> test folder", name="Quick export to test folder"
        )
        self._quick_export_btn.Bind(wx.EVT_BUTTON, lambda evt: self._start_export_project(test=True))
        self._open_output_btn = wx.Button(panel, label="Open output folder", name="Open output folder")
        self._open_output_btn.Bind(wx.EVT_BUTTON, self._on_open_output_folder)
        self._fx_report_btn = wx.Button(panel, label="FX report...", name="FX report")
        self._fx_report_btn.Bind(wx.EVT_BUTTON, self._on_fx_report)
        row.Add(self._export_project_btn, 0, wx.RIGHT, 6)
        row.Add(self._quick_export_btn, 0, wx.RIGHT, 6)
        row.Add(self._open_output_btn, 0, wx.RIGHT, 6)
        row.Add(self._fx_report_btn, 0)
        return row

    # -- helpers -------------------------------------------------------

    def _log_write(self, text: str) -> None:
        self._log.AppendText(text + "\n")

    def _project_path(self) -> str:
        return self._project_ctrl.GetValue().strip()

    def _output_dir_or_none(self) -> str | None:
        value = self._output_ctrl.GetValue().strip()
        return value or None

    def _output_name_or_none(self) -> str | None:
        value = self._output_name_ctrl.GetValue().strip()
        return value or None

    def _is_reverse(self) -> bool:
        return self._project_path().lower().endswith(".als")

    def _update_direction(self) -> None:
        """Relabel the controls for the direction the input file implies
        -- visible label AND accessible name, so a screen reader announces
        what is on screen."""

        def relabel(ctrl: wx.Control, label: str, name: str | None = None) -> None:
            ctrl.SetLabel(label)
            ctrl.SetName(name or label)

        if self._is_reverse():
            relabel(self._export_project_btn, "Export whole project -> .rpp", "Export whole project to .rpp")
            relabel(
                self._copy_samples_ctrl,
                f"Copy audio and drum samples into a {DEFAULT_MEDIA_SUBDIR} subfolder (portable export)",
                f"Copy audio and drum samples into a {DEFAULT_MEDIA_SUBDIR} subfolder",
            )
            relabel(self._scenes_ctrl, "Lay Session-view Scenes out as markers after the Arrangement")
        else:
            relabel(self._export_project_btn, "Export whole project -> .als", "Export whole project to .als")
            relabel(
                self._copy_samples_ctrl,
                "Copy samples into the export folder (portable export)",
                "Copy samples into the export folder",
            )
            relabel(self._scenes_ctrl, "Turn project markers into Session-view Scenes")
        self.Layout()

    def _update_button_states(self) -> None:
        have_project = bool(self._project_path()) and os.path.isfile(self._project_path())
        not_busy = not self._exporting
        self._export_project_btn.Enable(have_project and not_busy)
        self._quick_export_btn.Enable(have_project and not_busy)
        self._open_output_btn.Enable(self._last_output is not None and not_busy)
        self._fx_report_btn.Enable(self._last_report is not None and not_busy)
        # Standalone .adg files only exist in the .rpp -> .als direction.
        self._export_standalone_adg_ctrl.Enable(not self._is_reverse())

    def _output_path_for(self, ext: str, stem: str | None = None) -> str:
        rpp_path = self._project_path()
        base = stem or os.path.splitext(os.path.basename(rpp_path))[0]
        out_dir = self._output_dir_or_none() or os.path.dirname(os.path.abspath(rpp_path))
        return os.path.join(out_dir, base + ext)

    # -- events ----------------------------------------------------------

    def _on_project_changed(self, evt: wx.CommandEvent) -> None:
        self._update_direction()
        self._update_button_states()

    def _on_browse_project(self, evt: wx.CommandEvent) -> None:
        with wx.FileDialog(
            self,
            "Select project to convert",
            wildcard=(
                "REAPER project or Ableton Live Set (*.rpp;*.als)|*.rpp;*.RPP;*.als"
                "|REAPER project (*.rpp)|*.rpp;*.RPP|Ableton Live Set (*.als)|*.als|All files (*.*)|*.*"
            ),
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self._project_ctrl.SetValue(dlg.GetPath())
                self._update_button_states()

    def _on_browse_output(self, evt: wx.CommandEvent) -> None:
        with wx.DirDialog(self, "Select output folder") as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self._output_ctrl.SetValue(dlg.GetPath())

    def _set_busy(self, busy: bool, status: str) -> None:
        self._exporting = busy
        self.SetStatusText(status)
        self._update_button_states()

    def _run_export(self, fn, done_status: str) -> None:
        """`fn` returns (output path, FX report)."""
        # Log was cumulative across every export in the session -- after
        # exporting several projects back to back there was no way to
        # tell where one export's output ended and the next began, or
        # whether a re-export had actually happened at all (user-reported
        # 2026-07-29). Clear it per-export so it only ever shows the
        # CURRENT export's own output.
        self._log.Clear()
        self._set_busy(True, "Exporting...")

        def worker():
            buf = io.StringIO()
            output_path: str | None = None
            report: list[FxEntry] | None = None
            error: str | None = None
            try:
                with contextlib.redirect_stdout(buf):
                    output_path, report = fn()
            except SystemExit as e:
                error = str(e)
            except OSError as e:
                error = str(e)
            wx.CallAfter(self._on_export_finished, buf.getvalue(), output_path, report, error, done_status)

        threading.Thread(target=worker, daemon=True).start()

    def _on_export_finished(
        self,
        log_text: str,
        output_path: str | None,
        report: list[FxEntry] | None,
        error: str | None,
        done_status: str,
    ) -> None:
        if log_text:
            self._log_write(log_text.rstrip("\n"))
        if error:
            self._log_write(f"Error: {error}")
            self._set_busy(False, "Export failed -- see log.")
            return
        self._last_output = output_path
        self._last_report = report
        missing = sum(e.count for e in report or [] if not e.converted)
        if missing:
            done_status += f" {missing} FX not converted -- see FX report."
        self._set_busy(False, done_status)

    def _start_export_project(self, *, test: bool) -> None:
        if self._is_reverse():
            self._start_export_rpp(test=test)
            return
        path = self._project_path()
        name_override = self._output_name_or_none()
        if test:
            out_path = os.path.join(_LIVETEST_DIR, name_override + ".als") if name_override else None
        else:
            out_path = self._output_path_for(".als", stem=name_override)
        copy_samples = self._copy_samples_ctrl.GetValue()
        export_standalone_adg = self._export_standalone_adg_ctrl.GetValue()
        scenes = self._scenes_ctrl.GetValue()

        def run():
            report = export_project(
                path,
                out_path,
                test=test,
                copy_samples=copy_samples,
                export_standalone_adg=export_standalone_adg,
                scenes=scenes,
            )
            return (_LIVETEST_DIR if test else out_path), report

        self._run_export(run, "Project export done.")

    def _start_export_rpp(self, *, test: bool) -> None:
        path = self._project_path()
        stem = self._output_name_or_none() or os.path.splitext(os.path.basename(path))[0]
        if test:
            out_path = os.path.join(_LIVETEST_RPP_DIR, stem + ".rpp")
        else:
            out_path = self._output_path_for(".rpp", stem=stem)
        force = False
        if os.path.exists(out_path):
            # A real decision (the file could be a live REAPER project), so a dialog.
            answer = wx.MessageBox(
                f"{out_path}\nalready exists. Overwrite it?",
                "ReAbleton",
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
                self,
            )
            if answer != wx.YES:
                self.SetStatusText("Export cancelled -- output file already exists.")
                return
            force = True
        media_subdir = DEFAULT_MEDIA_SUBDIR if self._copy_samples_ctrl.GetValue() else None
        scenes = self._scenes_ctrl.GetValue()

        def run():
            report = export_rpp(path, out_path, scenes=scenes, force=force, media_subdir=media_subdir)
            return out_path, report

        self._run_export(run, "Export to REAPER done.")

    def _on_fx_report(self, evt: wx.CommandEvent) -> None:
        with FxReportDialog(self, self._last_report or []) as dlg:
            dlg.ShowModal()

    def _on_open_output_folder(self, evt: wx.CommandEvent) -> None:
        target = self._last_output or self._output_dir_or_none()
        if not target:
            wx.MessageBox("Nothing exported yet.", "ReAbleton", wx.ICON_INFORMATION)
            return
        folder = target if os.path.isdir(target) else os.path.dirname(target)
        if not os.path.isdir(folder):
            wx.MessageBox(f"Folder not found:\n{folder}", "ReAbleton", wx.ICON_INFORMATION)
            return
        os.startfile(folder)  # noqa: S606 -- Windows-only tool, matches the rest of this codebase


def main() -> int:
    app = wx.App()
    MainFrame().Show()
    app.MainLoop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
