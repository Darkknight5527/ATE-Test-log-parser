"""ATE Log Parser - desktop app.

The window is a local web page (app/ui) shown in a native window by pywebview (Edge WebView2 on Windows).
Python does all the work; the page calls the methods of `Api` below. If the web view cannot start
(e.g. WebView2 missing), the classic window (app/gui.py) opens instead.
"""
import datetime
import json
import os
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
BASE = getattr(sys, '_MEIPASS', os.path.dirname(HERE)) if getattr(sys, 'frozen', False) else os.path.dirname(HERE)
UI_DIR = os.path.join(sys._MEIPASS, 'ui') if getattr(sys, 'frozen', False) else os.path.join(HERE, 'ui')
sys.path.insert(0, BASE)

from ate_log_parser import __version__, profile as prof  # noqa: E402
from ate_log_parser.lot_trace import temperature_of  # noqa: E402
from ate_log_parser.report import build, collect  # noqa: E402
from ate_log_parser.stdf_reader import quick_info  # noqa: E402

STDF_TYPES = ('STDF files (*.stdf;*.std;*.stdf.gz;*.std.gz)', 'All files (*.*)')


def state_path():
    return os.path.join(os.path.dirname(prof.profiles_dir()), 'app_state.json')


def load_state():
    try:
        with open(state_path(), encoding='utf-8') as fh: return json.load(fh)
    except Exception:
        return {}


def save_state(st):
    try:
        os.makedirs(os.path.dirname(state_path()), exist_ok=True)
        with open(state_path(), 'w', encoding='utf-8') as fh: json.dump(st, fh, indent=1)
    except Exception:
        pass


class Api:
    def __init__(self):
        self.window = None
        self.busy = False

    # ---------- info
    def app_info(self):
        st = load_state()
        return dict(version=__version__, profiles=list(prof.list_profiles()), profile=st.get('profile'),
                    recent=st.get('recent', [])[:20], stats=st.get('stats', 'pass'))

    def profile_details(self, name):
        p = prof.load(prof.list_profiles().get(name))
        return {k: p.get(k) for k in ('name', 'readback_test', 'blown_bit_regex', 'compare_suites', 'device_regex',
                                       'temperature_rules', 'excluded_tags', 'traveller_sheets')}

    # ---------- dialogs
    def _dialog(self, kind, types=(), save_name=None, multiple=False):
        import webview
        k = {'open': webview.OPEN_DIALOG, 'folder': webview.FOLDER_DIALOG, 'save': webview.SAVE_DIALOG}[kind]
        kw = dict(allow_multiple=multiple, file_types=types)
        if save_name: kw = dict(save_filename=save_name, file_types=types)
        res = self.window.create_file_dialog(k, **kw)
        if not res: return []
        return [res] if isinstance(res, str) else list(res)

    def pick_files(self):
        return self._dialog('open', STDF_TYPES, multiple=True)

    def pick_folder(self):
        return self._dialog('folder')

    def pick_traveller(self):
        r = self._dialog('open', ('Excel workbook (*.xlsx;*.xlsm)', 'All files (*.*)'))
        return r[0] if r else ''

    def pick_output(self):
        r = self._dialog('save', ('Excel workbook (*.xlsx)',), save_name='STDF_Lot_Report.xlsx')
        return r[0] if r else ''

    def import_profile(self):
        r = self._dialog('open', ('Profile (*.json)',))
        if not r: return dict(ok=False)
        try:
            name = prof.install(r[0])
            return dict(ok=True, name=name, profiles=list(prof.list_profiles()))
        except Exception as e:
            return dict(ok=False, error=str(e))

    # ---------- files
    def scan(self, paths, profile_name=None):
        """List the STDF files under the given paths with their header info (fast)."""
        p = prof.load(prof.list_profiles().get(profile_name))
        out = []
        for path in collect(paths):
            try:
                mir = quick_info(path)
                fake = dict(mir=mir, path=path)
                out.append(dict(path=path, name=os.path.basename(path), folder=os.path.basename(os.path.dirname(path)),
                                size=os.path.getsize(path), lot=mir.get('lot', ''), job=mir.get('job', ''),
                                start=mir['start'].strftime('%Y-%m-%d %H:%M') if mir.get('start') else '',
                                stage=temperature_of(fake, p), ok=True))
            except Exception as e:
                out.append(dict(path=path, name=os.path.basename(path), folder=os.path.basename(os.path.dirname(path)),
                                size=os.path.getsize(path) if os.path.exists(path) else 0, ok=False, error=str(e)))
        out.sort(key=lambda x: (x.get('start') or '', x['name']))
        return out

    # ---------- run
    def generate(self, params):
        if self.busy: return dict(ok=False, error='A report is already running.')
        self.busy = True
        st = load_state(); st['profile'] = params.get('profile'); st['stats'] = params.get('stats', 'pass'); save_state(st)
        threading.Thread(target=self._run, args=(params,), daemon=True).start()
        return dict(ok=True)

    def _js(self, fn, payload):
        try:
            self.window.evaluate_js('%s(%s)' % (fn, json.dumps(payload, default=str)))
        except Exception:
            pass

    def _run(self, params):
        try:
            p = prof.load(prof.list_profiles().get(params.get('profile')))
            summary = {}
            outs, lines = build(params['paths'], params.get('output') or None, p, params.get('traveller') or None, None,
                                params.get('stats', 'pass'), log=lambda s: self._js('window.onLog', s), summary=summary)
            st = load_state()
            rec = dict(when=datetime.datetime.now().strftime('%Y-%m-%d %H:%M'), lot=summary.get('lot', ''), outputs=outs,
                       files=summary.get('files'), stages=summary.get('stages'))
            st['recent'] = [rec] + [r for r in st.get('recent', []) if r.get('outputs') != outs][:19]
            save_state(st)
            self._js('window.onDone', summary)
        except PermissionError:
            self._js('window.onError', 'Cannot write the output - is an older copy still open in WPS / Excel? Close it and try again.')
        except Exception as e:
            self._js('window.onError', '%s: %s' % (type(e).__name__, e))
        finally:
            self.busy = False

    def open_path(self, path):
        if not path or not os.path.exists(path): return False
        if sys.platform.startswith('win'): os.startfile(path)
        elif sys.platform == 'darwin': subprocess.Popen(['open', path])
        else: subprocess.Popen(['xdg-open', path])
        return True

    def open_folder(self, path):
        return self.open_path(os.path.dirname(path) if os.path.isfile(path) else path)


def main():
    try:
        import webview
    except Exception:
        return classic()
    api = Api()
    try:
        win = webview.create_window('ATE Log Parser %s' % __version__, url=os.path.join(UI_DIR, 'index.html'), js_api=api,
                                    width=1320, height=860, min_size=(980, 680), background_color='#0b0b0d')
        api.window = win

        def on_drop(e):   # files dropped on the window: pywebview gives full paths
            try:
                files = e['dataTransfer']['files']
                paths = [f.get('pywebviewFullPath') for f in files if f.get('pywebviewFullPath')]
                if paths: api._js('window.onDropPaths', paths)
            except Exception:
                pass

        def bind(window):
            try:
                from webview.dom import DOMEventHandler
                window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)
            except Exception:
                pass
        webview.start(bind, win, private_mode=False)
    except Exception:
        classic()


def classic():
    sys.path.insert(0, HERE)
    import gui
    gui.App().mainloop()


if __name__ == '__main__':
    main()
