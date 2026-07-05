"""
CADtributions
-------------
A Fusion 360 add-in that tracks every time you save a new version of a
design (or create a brand new file) and shows them in a GitHub-style
"contribution graph" -- a CADtribution graph.

Author: Sri
"""

import adsk.core
import adsk.fusion
import adsk.cam
import traceback
import os
import json
import datetime
import pathlib

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

_app: adsk.core.Application = None
_ui: adsk.core.UserInterface = None

# Fusion requires a reference to every event handler to be kept around for
# the lifetime of the add-in, otherwise Python garbage collects them and the
# events silently stop firing.
_handlers = []

ADDIN_NAME = 'CADtributions'
CMD_ID_SHOW_GRAPH = 'CADtributions_ShowGraph'
PALETTE_ID = 'CADtributions_Palette'
PALETTE_NAME = 'CADtributions'

ADDIN_FOLDER = os.path.dirname(os.path.realpath(__file__))
DATA_FILE_PATH = os.path.join(ADDIN_FOLDER, 'cadtributions_data.json')
HTML_FILE_PATH = os.path.join(ADDIN_FOLDER, 'palette.html')


# ---------------------------------------------------------------------------
# Data persistence
# ---------------------------------------------------------------------------
# Every "CADtribution" is stored as one JSON record:
#   {
#     "id":         unique id used to de-duplicate (fileId:version, or a
#                   local equivalent for files not saved to a Fusion project)
#     "timestamp":  full ISO-8601 timestamp of the save
#     "date":       YYYY-MM-DD (local date), used to bucket the graph
#     "project":    the Fusion "Data Panel" project name
#     "path":       breadcrumb path, e.g. "MyProject / Robots / Chassis"
#     "file":       the design's file name
#     "version":    the version number Fusion assigned to this save
#     "isNewFile":  True if this save created the file for the first time
#   }

def load_data() -> list:
    """Load all recorded CADtributions from disk. Never raises."""
    if not os.path.exists(DATA_FILE_PATH):
        return []
    try:
        with open(DATA_FILE_PATH, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:
                return []
            return json.loads(content)
    except (json.JSONDecodeError, OSError):
        # Corrupt or unreadable file -- back it up rather than losing data
        # silently, and start fresh so the add-in keeps working.
        try:
            backup_path = DATA_FILE_PATH + '.bak'
            os.replace(DATA_FILE_PATH, backup_path)
        except OSError:
            pass
        return []


def save_data(entries: list) -> None:
    tmp_path = DATA_FILE_PATH + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(entries, f, indent=2)
    os.replace(tmp_path, DATA_FILE_PATH)


def add_entry(entry: dict) -> bool:
    """Append a new CADtribution if it isn't already recorded.

    Returns True if a new entry was actually added (so callers can decide
    whether to refresh the UI), False if it was a duplicate.
    """
    entries = load_data()
    if any(e.get('id') == entry['id'] for e in entries):
        return False
    entries.append(entry)
    save_data(entries)
    return True


# ---------------------------------------------------------------------------
# Helpers for reading information out of the Fusion document / data file
# ---------------------------------------------------------------------------

def get_full_breadcrumb(data_file: adsk.core.DataFile) -> str:
    """Build a human readable 'Project / Folder / Subfolder' path."""
    try:
        parts = []
        folder = data_file.parentFolder
        while folder is not None:
            parts.insert(0, folder.name)
            folder = folder.parentFolder

        project_name = None
        try:
            project_name = data_file.parentProject.name
        except Exception:
            pass

        # Avoid "Project / Project" when the root folder repeats the
        # project's name.
        if project_name and (not parts or parts[0] != project_name):
            parts.insert(0, project_name)

        return ' / '.join(parts) if parts else (project_name or '')
    except Exception:
        return ''


def build_entry_for_saved_document(doc: adsk.core.Document) -> dict:
    """Turn a just-saved Document into a CADtribution record."""
    now = datetime.datetime.now()
    timestamp = now.isoformat(timespec='seconds')
    date_str = now.strftime('%Y-%m-%d')

    data_file = None
    try:
        data_file = doc.dataFile
    except Exception:
        data_file = None

    if data_file is not None:
        # Normal case: the document lives in a Fusion "Data Panel" project,
        # so Fusion is already tracking real version numbers for us.
        version = data_file.versionNumber
        entry_id = f'{data_file.id}:{version}'
        project_name = ''
        try:
            project_name = data_file.parentProject.name
        except Exception:
            project_name = 'Unknown Project'

        entry = {
            'id': entry_id,
            'timestamp': timestamp,
            'date': date_str,
            'project': project_name,
            'path': get_full_breadcrumb(data_file),
            'file': data_file.name,
            'version': version,
            'isNewFile': version == 1,
        }
    else:
        # Fallback for documents that aren't saved into a Fusion project
        # (rare in normal use, but possible). There's no real version
        # number available from the API here, so we derive one from what
        # we've already recorded for this file name.
        file_name = doc.name or 'Untitled'
        local_key = f'local::{file_name}'
        existing = [e for e in load_data() if e.get('id', '').startswith(local_key + ':')]
        version = len(existing) + 1
        entry = {
            'id': f'{local_key}:{version}:{timestamp}',
            'timestamp': timestamp,
            'date': date_str,
            'project': 'Local (not in a Fusion project)',
            'path': file_name,
            'file': file_name,
            'version': version,
            'isNewFile': version == 1,
        }

    return entry


# ---------------------------------------------------------------------------
# Palette (the CADtribution graph UI)
# ---------------------------------------------------------------------------

def get_palette() -> adsk.core.Palette:
    return _ui.palettes.itemById(PALETTE_ID)


def show_palette():
    palette = get_palette()
    if palette is None:
        html_uri = pathlib.Path(HTML_FILE_PATH).as_uri()
        palette = _ui.palettes.add(
            id=PALETTE_ID,
            name=PALETTE_NAME,
            htmlFileURL=html_uri,
            isVisible=True,
            showCloseButton=True,
            isResizable=True,
            width=920,
            height=680,
        )
        on_incoming = CADtributionsHTMLHandler()
        palette.incomingFromHTML.add(on_incoming)
        _handlers.append(on_incoming)

        on_closed = CADtributionsPaletteClosedHandler()
        palette.closed.add(on_closed)
        _handlers.append(on_closed)
    else:
        palette.isVisible = True

    send_data_to_palette()


def send_data_to_palette():
    """Push the latest CADtribution history into the open palette, if any."""
    palette = get_palette()
    if palette is not None and palette.isVisible:
        data = load_data()
        palette.sendInfoToHTML('populate', json.dumps(data))


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

class DocumentSavedHandler(adsk.core.DocumentEventHandler):
    """Fires every time any document is saved -- this is where a
    CADtribution actually gets recorded."""

    def notify(self, args: adsk.core.DocumentEventArgs):
        try:
            doc = args.document
            entry = build_entry_for_saved_document(doc)
            added = add_entry(entry)
            if added:
                send_data_to_palette()
        except Exception:
            # Never let a failure here interrupt the user's save.
            if _ui:
                _ui.messageBox(
                    'CADtributions failed to record a save:\n{}'.format(
                        traceback.format_exc()
                    )
                )


class ShowGraphCommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    """Fires when the toolbar button is clicked."""

    def notify(self, args: adsk.core.CommandCreatedEventArgs):
        try:
            show_palette()
        except Exception:
            if _ui:
                _ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))


class CADtributionsHTMLHandler(adsk.core.HTMLEventHandler):
    """Fires when the palette's JavaScript calls adsk.fusionSendData(...)."""

    def notify(self, args):
        try:
            html_args = adsk.core.HTMLEventArgs.cast(args)
            action = html_args.action

            if action in ('ready', 'refresh'):
                data = load_data()
                html_args.returnData = json.dumps(data)
            else:
                html_args.returnData = json.dumps({'status': 'ignored'})
        except Exception:
            if _ui:
                _ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))


class CADtributionsPaletteClosedHandler(adsk.core.UserInterfaceGeneralEventHandler):
    def notify(self, args):
        pass


# ---------------------------------------------------------------------------
# Add-in lifecycle
# ---------------------------------------------------------------------------

def _initialize():
    cmd_def = _ui.commandDefinitions.itemById(CMD_ID_SHOW_GRAPH)
    if not cmd_def:
        cmd_def = _ui.commandDefinitions.addButtonDefinition(
            CMD_ID_SHOW_GRAPH,
            'CADtributions',
            'Show your CADtribution graph -- a GitHub-style view of your\n'
            'Fusion 360 save history: what you saved, where, and when.',
            './resources/graph_cmd',
        )

    on_created = ShowGraphCommandCreatedHandler()
    cmd_def.commandCreated.add(on_created)
    _handlers.append(on_created)

    workspace = _ui.workspaces.itemById('FusionSolidEnvironment')
    panel = workspace.toolbarPanels.itemById('SolidScriptsAddinsPanel')
    if panel.controls.itemById(CMD_ID_SHOW_GRAPH) is None:
        control = panel.controls.addCommand(cmd_def)
        control.isPromoted = False

    on_saved = DocumentSavedHandler()
    _app.documentSaved.add(on_saved)
    _handlers.append(on_saved)


def _cleanup():
    try:
        workspace = _ui.workspaces.itemById('FusionSolidEnvironment')
        panel = workspace.toolbarPanels.itemById('SolidScriptsAddinsPanel')
        control = panel.controls.itemById(CMD_ID_SHOW_GRAPH)
        if control:
            control.deleteMe()
    except Exception:
        pass

    try:
        cmd_def = _ui.commandDefinitions.itemById(CMD_ID_SHOW_GRAPH)
        if cmd_def:
            cmd_def.deleteMe()
    except Exception:
        pass

    try:
        palette = get_palette()
        if palette:
            palette.deleteMe()
    except Exception:
        pass

    _handlers.clear()


def run(context):
    global _app, _ui
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface
        _initialize()
    except Exception:
        if _ui:
            _ui.messageBox(
                'CADtributions failed to start:\n{}'.format(traceback.format_exc())
            )


def stop(context):
    try:
        _cleanup()
    except Exception:
        if _ui:
            _ui.messageBox(
                'CADtributions failed to stop:\n{}'.format(traceback.format_exc())
            )