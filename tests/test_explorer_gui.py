"""Real Tk smoke test; release Windows runner supplies a desktop session."""
import os
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'desktop'))
from pages_explorer import ExplorerPage
from remote_files import REMOTE_FILE_PROGRAM
from state import AppState


class ExplorerGuiTests(unittest.TestCase):
    def test_navigate_select_edit_and_permissions(self):
        try:
            if os.name == 'nt':
                from tkinterdnd2 import TkinterDnD
                root = TkinterDnD.Tk()
            else: root = tk.Tk()
        except tk.TclError as error:
            if os.name == 'nt': raise
            self.skipTest(str(error))
        root.withdraw()
        page = None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                (base/'pack.1.2').mkdir()
                (base/'pack.1.2/config.txt').write_text('first', encoding='utf-8')
                ns = {'__name__':'gui_fixture'}
                exec(REMOTE_FILE_PROGRAM, ns)
                ns['ROOT'] = base
                def run_async(work, success, failure=None):
                    success(work())  # Fail the test rather than displaying a blocking error dialog.
                panel = SimpleNamespace(state=AppState({'role':'owner'}), run_async=run_async, status=lambda *a,**kw:None)
                page = ExplorerPage(root, panel)
                page.request = lambda action, **kw: ns['dispatch'](dict(action=action, **kw))
                page.pack(fill='both', expand=True)
                page.on_show()
                root.update()
                self.assertEqual(len(page.entries), 1)
                page.tree.selection_set('0')
                page.open_selected()
                self.assertEqual(page.path, 'pack.1.2')
                page.tree.selection_set('0')
                page.open_selected()
                root.update()
                self.assertEqual(len(page.editors), 1)
                page.go_up()
                self.assertEqual(page.path, '')
                page.go_back()
                self.assertEqual(page.path, 'pack.1.2')
                page.go_forward()
                self.assertEqual(page.path, '')
                page.search.set('.missing')
                page.refresh(reset_page=True)
                self.assertFalse(page.entries)
                page.search.set('')
                page.refresh()
                page.tree.selection_set('0')
                page.copy('copy')
                self.assertEqual(page.clipboard[0], 'copy')
                panel.state.user = {'role':'user', 'permissions':['terminal.linux','minecraft.files.read']}
                page.set_busy(False)
                self.assertTrue(all(str(button.cget('state')) == 'disabled' for button, write in page.buttons if write))
        finally:
            if page: page.close()
            root.destroy()


if __name__ == '__main__': unittest.main()
