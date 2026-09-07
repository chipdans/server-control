"""Exercise remote operations on disposable real files, including aborted writes."""
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'desktop'))
from remote_files import REMOTE_FILE_PROGRAM
from direct_files import file_command
from pages_explorer import child_path


class ExplorerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'minecraft'
        self.root.mkdir()
        self.ns = {'__name__': 'test_remote_files'}
        exec(compile(REMOTE_FILE_PROGRAM, '<remote_files>', 'exec'), self.ns)
        self.ns['ROOT'] = self.root
        self.run = self.ns['dispatch']
        self.version = self.ns['version']

    def tearDown(self): self.tmp.cleanup()

    def test_pagination_sort_search_and_hidden(self):
        (self.root/'mods').mkdir()
        for name in ['a.txt', 'b.jar', 'c.jar', '.secret']:
            (self.root/name).write_text(name)
        result = self.run(dict(action='list', path='', page_size=2))
        self.assertEqual(result['total'], 4)
        self.assertEqual(result['entries'][0]['name'], 'mods')
        self.assertEqual(self.run(dict(action='list', path='', search='.jar'))['total'], 2)
        self.assertEqual(self.run(dict(action='list', path='', page=2, page_size=2))['page'], 2)
        self.assertEqual(self.run(dict(action='list', path='', hidden=True))['total'], 5)

    def test_create_edit_and_concurrent_change(self):
        self.run(dict(action='mkdir', path='pack'))
        self.run(dict(action='write', path='pack/конфиг.txt', expected=None, content='первая версия'))
        info = self.run(dict(action='read', path='pack/конфиг.txt'))
        self.assertEqual(info['content'], 'первая версия')
        (self.root/'pack/конфиг.txt').write_text('изменено другим пользователем', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            self.run(dict(action='write', path='pack/конфиг.txt', expected=info['version'], content='не заменять'))
        self.assertEqual((self.root/'pack/конфиг.txt').read_text(encoding='utf-8'), 'изменено другим пользователем')

    def test_copy_move_and_collision(self):
        p = self.root/'a.txt'; p.write_text('data')
        self.run(dict(action='copy', path='a.txt', destination='b.txt', source_version=self.version(p)))
        self.assertEqual(p.read_text(), (self.root/'b.txt').read_text())
        with self.assertRaises(FileExistsError):
            self.run(dict(action='move', path='a.txt', destination='b.txt', source_version=self.version(p)))
        self.run(dict(action='move', path='a.txt', destination='новое имя.txt', source_version=self.version(p)))
        self.assertFalse(p.exists())
        self.assertEqual((self.root/'новое имя.txt').read_text(), 'data')

    def test_delete_restore_and_restore_conflict(self):
        p = self.root/'pack'; p.mkdir(); (p/'world.dat').write_bytes(b'world')
        token = self.run(dict(action='delete', path='pack', expected=self.version(p)))['token']
        self.assertFalse(p.exists())
        self.assertEqual(self.run(dict(action='list', path='', hidden=True))['total'], 0)
        p.mkdir()
        with self.assertRaises(FileExistsError): self.run(dict(action='restore', token=token))
        p.rmdir()
        self.run(dict(action='restore', token=token))
        self.assertEqual((p/'world.dat').read_bytes(), b'world')

    def test_paths_root_and_symlinks(self):
        for value in ['../outside', '/etc/passwd', 'pack/../../etc', '.server-control-trash', '.sc-transfer-123']:
            with self.assertRaises(ValueError): self.ns['safe'](value)
        with self.assertRaises(ValueError): self.run(dict(action='delete', path='', expected=None))
        if os.name != 'nt':
            (self.root/'link').symlink_to(self.root.parent)
            with self.assertRaises(ValueError): self.ns['safe']('link/outside')
            pack = self.root/'pack'; pack.mkdir(); (pack/'outside').symlink_to('/etc/passwd')
            with self.assertRaises(ValueError): self.run(dict(action='copy', path='pack', destination='copy', source_version=self.version(pack)))
            self.assertFalse((self.root/'copy').exists())

    def invoke(self, payload, content=b''):
        # The same program and framing as SSH; only the fixed root changes for tests.
        program = REMOTE_FILE_PROGRAM.replace("ROOT = Path('/opt/minecraft')", 'ROOT = Path(' + repr(str(self.root)) + ')')
        return subprocess.run([sys.executable, '-c', program, base64.b64encode(json.dumps(payload).encode()).decode()], input=content, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)

    def test_streamed_upload_commit_and_cancellation(self):
        p = self.root/'data.bin'; p.write_bytes(b'original')
        req = dict(action='upload', path='data.bin', expected=self.version(p), size=3)
        for content in [b'x', b'new', b'newX']:
            result = self.invoke(req, content)
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertEqual(p.read_bytes(), b'original')
            self.assertFalse(list(self.root.glob('.sc-transfer-*')))
        result = self.invoke(req, b'newC')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(p.read_bytes(), b'new')

    def test_binary_and_directory_download(self):
        p = self.root/'pack'; p.mkdir(); (p/'empty').mkdir(); (p/'data.bin').write_bytes(bytes(range(256))*100)
        direct = self.invoke(dict(action='download', path='pack/data.bin'))
        header, body = direct.stdout.split(b'\n',1)
        self.assertEqual(direct.returncode, 0, direct.stderr)
        self.assertEqual(json.loads(header)['size'], len(body))
        self.assertEqual(body, (p/'data.bin').read_bytes())
        archive = self.invoke(dict(action='download', path='pack'))
        self.assertEqual(archive.returncode, 0, archive.stderr)
        header, body = archive.stdout.split(b'\n',1)
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            self.assertIsNone(z.testzip())
            self.assertIn('pack/empty/', z.namelist())
            self.assertEqual(z.read('pack/data.bin'), (p/'data.bin').read_bytes())

    def test_copy_own_descendant_and_command_quoting(self):
        p = self.root/'pack'; p.mkdir()
        with self.assertRaises(ValueError): self.run(dict(action='copy', path='pack', destination='pack/child', source_version=self.version(p)))
        import shlex
        request = dict(action='list', path="папка ' ; $(touch nope)")
        command = shlex.split(file_command(request))
        self.assertEqual(json.loads(base64.b64decode(command[-1])), request)
        self.assertEqual(child_path('pack 1.2', 'file.txt'), 'pack 1.2/file.txt')
        with self.assertRaises(ValueError): child_path('pack', '../escape')


if __name__ == '__main__': unittest.main()
