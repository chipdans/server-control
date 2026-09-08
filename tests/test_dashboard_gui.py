"""Exercise the real dashboard at a small window size on the Windows builder."""

import os
import sys
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
from pages_dashboard_v2 import DashboardPage
from state import AppState


class DashboardGuiTests(unittest.TestCase):
    def test_metrics_scroll_and_reset_on_switch_and_disconnect(self):
        try:
            root = tk.Tk()
        except tk.TclError as error:
            if os.name == "nt":
                raise
            self.skipTest(str(error))
        page = None
        try:
            # Apply the same style and available width as the actual small window.
            from main import ServerControlApp
            ServerControlApp._configure_style(SimpleNamespace(root=root), "dark")
            root.geometry("790x520")
            state = AppState({"role": "owner"})
            panel = SimpleNamespace(state=state, refresh_now=lambda: None, restart_minecraft=lambda: None)
            page = DashboardPage(root, panel)
            page.pack(fill="both", expand=True)
            performance = {"status": "ok", "tps": 20.0, "mspt": 15.0, "age_seconds": 0, "measured_at": 1000}
            instance = {"id": "one", "name": "Первая сборка", "state": "RUNNING", "startup": {"start_id": 1}, "performance": performance, "process": {"pid": 42, "cpu_percent": 15.0, "memory_bytes": 1024 ** 3}}
            status = {"selected_instance_id": "one", "instances": [instance], "server": {"metrics": {"collected_at": 1000}}}
            state.server = {"online": True, "status": status}
            state.connected = True
            page.update_state()
            root.update()
            self.assertEqual(page.tps.value.get(), "20.0 / 20")
            self.assertEqual(page.java_cpu.value.get(), "15.0%")
            self.assertEqual(len(page.tps.history), 1)
            performance.update(mspt=25, measured_at=2000)
            page.update_state()
            root.update()
            self.assertEqual(len(page.mspt.history), 2)
            for card in (page.tps, page.mspt, page.java_cpu, page.java_memory):
                self.assertLessEqual(card.detail_label.winfo_x() + card.detail_label.winfo_width(), card.winfo_width())
                self.assertLessEqual(card.detail_label.winfo_y() + card.detail_label.winfo_height(), card.graph.winfo_y())
            self.assertLess(page.canvas.yview()[1], 1)
            page._scroll(SimpleNamespace(delta=-120, num=None))
            root.update()
            self.assertGreater(page.canvas.yview()[0], 0)
            instance.update(id="two", name="Вторая сборка", startup={"start_id": 2})
            status["selected_instance_id"] = "two"
            page.update_state()
            root.update()
            self.assertIn("Вторая сборка", page.minecraft.title.get())
            self.assertEqual(len(page.mspt.history), 1)
            state.connected = False
            page.update_state()
            root.update()
            self.assertEqual(page.tps.value.get(), "—")
            self.assertEqual(page.java_cpu.value.get(), "—")
            self.assertFalse(page.tps.history)
        finally:
            if page:
                page.destroy()
            root.destroy()


if __name__ == "__main__":
    unittest.main()
