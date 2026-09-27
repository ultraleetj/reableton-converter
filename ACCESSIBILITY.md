# GUI Accessibility Spec

Screen-reader accessibility rules for `rpp_to_als`'s GUI. Trimmed down from
a prior wxPython project's spec (LyPlex) — kept the library choice,
techniques, and forbidden patterns; dropped the i18n/language-switching
machinery (`S()`, `_i18n` list) since this tool has no localization
requirement. Apply these to all GUI code (`rpp_to_als/gui.py` or similar).

## Library choice: wxPython

Use `wxPython`, not Tkinter or a web-view wrapper. wxPython wraps native
Win32 controls, which is what makes NVDA/JAWS/UIA work correctly out of the
box; Tkinter's controls have much weaker accessible-name support on
Windows, and a web-based GUI (Electron/pywebview) requires re-solving all
of this via ARIA instead of reusing native semantics for free.

## Screen Reader Labels

Every interactive control needs an accessible name via `name=` in the
constructor. Applies to all interactive widget types: `wx.SpinCtrl`,
`wx.Choice`, `wx.TextCtrl`, `wx.Button`.

```python
ctrl = wx.SpinCtrl(panel, ..., name="Note")
```

A control with both a visible label and an accessible name needs both set
— visible label via `label=`/`SetLabel`, accessible name via `name=`/
`SetName()`. `wx.StaticText` proximity to a control is **not** enough;
UIA does not auto-associate a preceding label with the next `TextCtrl` or
`Choice` — `SetName()` is required on the control itself.

## Label-Before-Control DOM Order

`wx.StaticText` labels must be added to the sizer **before** their
associated control. UIA/NVDA traverse focus order by widget creation
order — label added after control breaks the association.

```python
lbl = wx.StaticText(panel, label="Track")   # created first
ctrl = wx.Choice(panel, choices=track_names)  # created second
grid.Add(lbl, ...)
grid.Add(ctrl, ...)
```

## SpinCtrl vs SpinCtrlDouble

Use `wx.SpinCtrl` (integer) for any screen-reader-accessible spin field
(e.g. MIDI note number, choke group number).

**Never use `wx.SpinCtrlDouble` in the main window.** It's a composite
Windows control — NVDA/JAWS focus the inner edit HWND, which does not
inherit the accessible name set on the outer container (`SetName()`,
`SetHelpText()`, `name=` all fail to reach it).

For float values needing decimal input (unlikely here — this tool's
numeric fields are mostly MIDI notes/integers), use a
`wx.TextCtrl` + `wx.SpinButton` pair instead, clamped on `EVT_KILL_FOCUS`.
The `wx.TextCtrl` in that pair still needs `name=` set; the `SpinButton`
doesn't (it's a visual nudge only, the text field is the real control).

## Browse Button Uniqueness

Every file/dir picker row (`.rpp` input, `.adg` output) produces a
"Browse" button. If several exist, give each a distinct accessible name —
the field's label, not the generic word "Browse" — so a screen reader
announces "REAPER project Browse button" / "Output folder Browse button"
instead of several indistinguishable "Browse" buttons. Visible label can
stay "Browse".

## Color / Choice Selection

Prefer `wx.Choice` with named presets over any non-keyboard-navigable
picker control (e.g. `wx.ColourPickerCtrl`) for any GUI element in this
tool that needs one — applies generally, even though this tool likely has
no color pickers today.

## Status Bar, Not MessageBox, for Status

Use `wx.Frame.CreateStatusBar()` + `SetStatusText()` for pipeline state
changes ("Parsing project...", "Exporting pad 3/12...", "Done", "Failed:
..."). Screen readers announce status bar text automatically.
`wx.MessageBox` interrupts with a modal for things that aren't actually
decisions — reserve message boxes for real confirmations/errors requiring
a response, not routine progress updates.

## Log TextCtrl

Give the log an accessible name:

```python
self._log = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY
                         | wx.TE_DONTWRAP | wx.HSCROLL, name="Export log")
```

Monospace font. Screen readers can navigate log lines with arrow keys once
focused.

## Button States

Reflect enabled/disabled state accurately, always. Example for this tool:

| Button | Enabled when |
|--------|-------------|
| Export | A track with RS5k instances is selected, not currently exporting |
| Cancel | Only while exporting |
| Open output folder | After a successful export |

Use `Enable()`/`Disable()` — never `Hide()` to represent unavailability.
Hidden controls break tab order for keyboard/screen-reader navigation.

## Dialog Buttons

Use `wx.Dialog.CreateButtonSizer(wx.OK | wx.CANCEL)` for dialogs — gives
correct keyboard handling (Enter = OK, Escape = Cancel) for free.

## Grid Layout

3-column `FlexGridSizer`: label | control | hint/unit.
`AddGrowableCol(1, 1)` so the control column stretches. Use
`grid.AddSpacer(0)` for empty cells — never skip a cell, or the column
count desyncs.

## Summary of Forbidden Patterns

| Pattern | Reason | Use instead |
|---------|--------|-------------|
| `wx.SpinCtrlDouble` in main window | Inner HWND hides accessible name | `wx.SpinCtrl` (int) |
| `wx.ColourPickerCtrl` | Not keyboard-navigable | `wx.Choice` + swatch panel |
| `wx.Choice`/`wx.TextCtrl` without `name=` | Screen reader sees unnamed control | Always set `name=` |
| Browse buttons with only a generic label | Indistinguishable "Browse" buttons | Distinct `name=` per field |
| Label added after control | Wrong UIA traversal order | Label first, control second |
| `Hide()` instead of `Disable()` | Breaks tab order | `Enable(False)` |
| `wx.MessageBox` for routine status | Not screen-reader-friendly, interrupts | `SetStatusText()` on status bar |
