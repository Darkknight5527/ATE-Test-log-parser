"""ATE Log Parser - window version (built into an .exe by GitHub Actions / build_exe.bat).

Add the STDF files (or folders) of ONE lot, pick the device profile and optionally the lot traveller,
then Generate: you get the lot report, and a copy of the traveller with an 'STDF Fill-in' sheet.
"""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ate_log_parser import __version__, profile as prof  # noqa: E402
from ate_log_parser.report import build  # noqa: E402

APP = 'ATE Log Parser %s' % __version__


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP); self.geometry('960x720'); self.minsize(820, 600)
        self.paths, self.q, self.outputs = [], queue.Queue(), []
        top = ttk.Frame(self, padding=10); top.pack(fill='both', expand=True)

        ttk.Label(top, text='1.  STDF files / folders of ONE lot  (folders include sub-folders)', font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        lf = ttk.Frame(top); lf.pack(fill='both', expand=True, pady=(4, 6))
        self.lst = tk.Listbox(lf, selectmode='extended', font=('Consolas', 9))
        sb = ttk.Scrollbar(lf, orient='vertical', command=self.lst.yview); self.lst.configure(yscrollcommand=sb.set)
        self.lst.pack(side='left', fill='both', expand=True); sb.pack(side='right', fill='y')
        bf = ttk.Frame(top); bf.pack(fill='x')
        ttk.Button(bf, text='Add files...', command=self.add_files).pack(side='left')
        ttk.Button(bf, text='Add folder...', command=self.add_folder).pack(side='left', padx=6)
        ttk.Button(bf, text='Remove selected', command=self.remove_sel).pack(side='left')
        ttk.Button(bf, text='Clear', command=self.clear).pack(side='left', padx=6)

        of = ttk.LabelFrame(top, text='2.  Settings', padding=8); of.pack(fill='x', pady=8)
        ttk.Label(of, text='Device profile:').grid(row=0, column=0, sticky='w')
        self.profiles = prof.list_profiles()
        self.pv = tk.StringVar(value=self._last_profile())
        self.pcb = ttk.Combobox(of, textvariable=self.pv, values=list(self.profiles), width=48, state='readonly')
        self.pcb.grid(row=0, column=1, columnspan=2, sticky='w')
        ttk.Button(of, text='Import profile...', command=self.import_profile).grid(row=0, column=3, padx=4)
        ttk.Label(of, text='(tells the tool which test identifies a part; Generic = no part tracking across files)',
                  foreground='#666').grid(row=1, column=1, columnspan=4, sticky='w')

        ttk.Label(of, text='Lot traveller (optional):').grid(row=2, column=0, sticky='w', pady=(8, 0))
        self.trav = tk.StringVar()
        ttk.Entry(of, textvariable=self.trav, width=70).grid(row=2, column=1, columnspan=2, sticky='we', pady=(8, 0))
        ttk.Button(of, text='Browse...', command=self.pick_trav).grid(row=2, column=3, padx=4, pady=(8, 0))
        ttk.Label(of, text='(a COPY gets an "STDF Fill-in" sheet; your traveller is not changed)', foreground='#666').grid(row=3, column=1, columnspan=4, sticky='w')

        ttk.Label(of, text='Save report to:').grid(row=4, column=0, sticky='w', pady=(8, 0))
        self.out = tk.StringVar()
        ttk.Entry(of, textvariable=self.out, width=70).grid(row=4, column=1, columnspan=2, sticky='we', pady=(8, 0))
        ttk.Button(of, text='Browse...', command=self.pick_out).grid(row=4, column=3, padx=4, pady=(8, 0))
        ttk.Label(of, text='(empty = next to the first STDF, STDF_Lot_Report_<date>_<time>.xlsx)', foreground='#666').grid(row=5, column=1, columnspan=4, sticky='w')

        self.stats = tk.StringVar(value='pass')
        ttk.Label(of, text='Statistics on:').grid(row=6, column=0, sticky='w', pady=(8, 0))
        ttk.Radiobutton(of, text='PASS units (like the viewer)', variable=self.stats, value='pass').grid(row=6, column=1, sticky='w', pady=(8, 0))
        ttk.Radiobutton(of, text='ALL units', variable=self.stats, value='all').grid(row=6, column=2, sticky='w', pady=(8, 0))

        rf = ttk.Frame(top); rf.pack(fill='x')
        self.btn = ttk.Button(rf, text='3.  Generate', command=self.run); self.btn.pack(side='left')
        self.open_btn = ttk.Button(rf, text='Open report', command=lambda: self.open(0), state='disabled'); self.open_btn.pack(side='left', padx=6)
        self.open_t = ttk.Button(rf, text='Open traveller copy', command=lambda: self.open(1), state='disabled'); self.open_t.pack(side='left')
        self.pb = ttk.Progressbar(rf, mode='indeterminate'); self.pb.pack(side='left', fill='x', expand=True, padx=6)
        self.log = tk.Text(top, height=12, font=('Consolas', 9), state='disabled', background='#f7f7f7')
        self.log.pack(fill='both', expand=False, pady=(8, 0))
        self.after(100, self.poll)

    # ---------------- settings helpers
    def _cfg_path(self):
        return os.path.join(os.path.dirname(prof.profiles_dir()), 'gui.txt')

    def _last_profile(self):
        try:
            name = open(self._cfg_path(), encoding='utf-8').read().strip()
            if name in self.profiles: return name
        except Exception:
            pass
        return next(iter(self.profiles))

    def import_profile(self):
        p = filedialog.askopenfilename(title='Select a profile JSON', filetypes=[('Profile', '*.json')])
        if not p: return
        try:
            name = prof.install(p)
        except Exception as e:
            messagebox.showerror(APP, 'Not a valid profile:\n%s' % e); return
        self.profiles = prof.list_profiles(); self.pcb['values'] = list(self.profiles); self.pv.set(name)

    # ---------------- inputs
    def _add(self, items):
        for p in items:
            p = os.path.normpath(p)
            if p not in self.paths:
                self.paths.append(p); self.lst.insert('end', ('[folder] ' if os.path.isdir(p) else '') + p)

    def add_files(self):
        self._add(filedialog.askopenfilenames(title='Select STDF files', filetypes=[('STDF files', '*.stdf *.std *.stdf.gz *.std.gz'), ('All files', '*.*')]))

    def add_folder(self):
        d = filedialog.askdirectory(title='Select folder with STDF files')
        if d: self._add([d])

    def remove_sel(self):
        for i in reversed(self.lst.curselection()): self.lst.delete(i); del self.paths[i]

    def clear(self):
        self.lst.delete(0, 'end'); self.paths = []

    def pick_trav(self):
        p = filedialog.askopenfilename(title='Select the lot traveller', filetypes=[('Excel', '*.xlsx')])
        if p: self.trav.set(p)

    def pick_out(self):
        p = filedialog.asksaveasfilename(defaultextension='.xlsx', filetypes=[('Excel workbook', '*.xlsx')], initialfile='STDF_Lot_Report.xlsx')
        if p: self.out.set(p)

    # ---------------- run
    def write(self, s):
        self.log.configure(state='normal'); self.log.insert('end', s + '\n'); self.log.see('end'); self.log.configure(state='disabled')

    def run(self):
        if not self.paths:
            messagebox.showwarning(APP, 'Add the STDF files of one lot first.'); return
        try:
            os.makedirs(os.path.dirname(self._cfg_path()), exist_ok=True)
            open(self._cfg_path(), 'w', encoding='utf-8').write(self.pv.get())
        except Exception:
            pass
        self.btn.configure(state='disabled'); self.open_btn.configure(state='disabled'); self.open_t.configure(state='disabled')
        self.log.configure(state='normal'); self.log.delete('1.0', 'end'); self.log.configure(state='disabled')
        self.pb.start(12)
        p = prof.load(self.profiles.get(self.pv.get()))
        args = (list(self.paths), self.out.get().strip() or None, p, self.trav.get().strip() or None, None, self.stats.get())
        threading.Thread(target=self.worker, args=args, daemon=True).start()

    def worker(self, *args):
        try:
            outs, _ = build(*args, log=lambda s: self.q.put(('log', s)))
            self.q.put(('done', outs))
        except PermissionError:
            self.q.put(('err', 'Cannot write the output - is an old copy still open in WPS/Excel? Close it and try again.'))
        except Exception as e:
            self.q.put(('err', '%s: %s' % (type(e).__name__, e)))

    def poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == 'log':
                    self.write(val)
                else:
                    self.pb.stop(); self.btn.configure(state='normal')
                    if kind == 'done':
                        self.outputs = val; self.open_btn.configure(state='normal')
                        if len(val) > 1: self.open_t.configure(state='normal')
                        self.write('\nDone.')
                        if messagebox.askyesno(APP, 'Report ready:\n%s\n\nOpen it now?' % val[0]): self.open(0)
                    else:
                        self.write('\nERROR: ' + val); messagebox.showerror(APP, val)
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def open(self, i):
        if len(self.outputs) <= i: return
        p = self.outputs[i]
        if sys.platform.startswith('win'): os.startfile(p)
        else: subprocess.Popen(['xdg-open', p])


if __name__ == '__main__':
    App().mainloop()
