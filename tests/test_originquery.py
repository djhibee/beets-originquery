import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from beets import config
from beets.library import Item, Library

from beetsplug.originquery import OriginQuery


class ImportTask:
    is_album = True

    def __init__(self, path, items):
        self.paths = [os.fsencode(path)]
        self.items = items

    def imported_items(self):
        return self.items


class OriginImportTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        config.clear()
        config.read(user=False)
        self.addCleanup(config.clear)
        config['musicbrainz']['extra_tags'] = ['media', 'catalognum']

    def plugin(self, filename='origin.json', patterns=None):
        config['originquery'].set({
            'origin_file': filename,
            'tag_patterns': {} if patterns is None else patterns,
        })
        plugin = OriginQuery()
        self.assertTrue(hasattr(plugin, 'tasks'))
        return plugin

    def task(self, **fields):
        return ImportTask(self.path, [
            Item(path=os.fsencode(self.path / 'track.flac'), **fields),
            Item(path=os.fsencode(self.path / 'track2.flac'), **fields),
        ])

    def test_default_json_origin(self):
        (self.path / 'origin.json').write_text(json.dumps({'Origin': 'Ripper A'}))
        task = self.task()
        plugin = self.plugin()
        plugin.import_task_start(task, None)
        self.assertEqual(plugin.tasks[task]['origin'], 'Ripper A')
        self.assertTrue(all('origin' not in item for item in task.items))
        self.assertNotIn('import_source', task.items[0])

    def test_default_yaml_origin(self):
        (self.path / 'origin.yaml').write_text('Origin: Ripper B\n')
        task = self.task()
        plugin = self.plugin('origin.yaml')
        plugin.import_task_start(task, None)
        self.assertEqual(plugin.tasks[task]['origin'], 'Ripper B')

    def test_overridden_json_pattern(self):
        (self.path / 'origin.json').write_text(json.dumps({
            'Origin': 'Wrong source', 'rip': {'source': 'Ripper C'},
        }))
        task = self.task()
        plugin = self.plugin(patterns={'origin': '$.rip.source'})
        plugin.import_task_start(task, None)
        self.assertEqual(plugin.tasks[task]['origin'], 'Ripper C')

    def test_text_origin(self):
        (self.path / 'origin.txt').write_text('Ripped by: Ripper D\n')
        task = self.task()
        plugin = self.plugin('origin.txt', {'origin': r'Ripped by: (.+)'})
        plugin.import_task_start(task, None)
        self.assertEqual(plugin.tasks[task]['origin'], 'Ripper D')

    def test_parser_debug_logs(self):
        for filename, contents, parser_type, patterns in (
                ('origin.json', '{"Origin": "Ripper"}', 'JSON', {}),
                ('origin.yaml', 'Origin: Ripper\n', 'YAML', {}),
                ('origin.txt', 'Origin=Ripper\n', 'text',
                 {'origin': r'Origin=(.+)'})):
            with self.subTest(filename=filename):
                origin_path = self.path / filename
                origin_path.write_text(contents)
                plugin = self.plugin(filename, patterns)
                with patch.object(plugin._log, 'debug') as debug:
                    self.assertEqual(list(plugin.match_fn(origin_path)),
                                     [('origin', 'Ripper')])
                debug.assert_any_call(
                    'Parsing origin file {} as ' + parser_type, origin_path)
                debug.assert_any_call(
                    'Origin field {} extracted value: {!r}', 'origin', 'Ripper')
                self.assertTrue(any(
                    call.args[0] == 'Origin field {} pattern {}: {} matches'
                    and call.args[1] == 'origin' and call.args[3] == 1
                    for call in debug.call_args_list))

    def test_missing_origin_debug_logs(self):
        (self.path / 'origin.json').write_text('{}')
        plugin = self.plugin()
        task = self.task()
        with patch.object(plugin._log, 'debug') as debug:
            plugin.import_task_start(task, None)
            plugin.import_origin(None, task)
        debug.assert_any_call(
            'Searching for origin files with {}: {}',
            str(self.path / 'origin.json'), [str(self.path / 'origin.json')])
        self.assertTrue(any(
            call.args[0] == 'Origin field {} pattern {}: {} matches'
            and call.args[1] == 'origin' and call.args[3] == 0
            for call in debug.call_args_list))
        debug.assert_any_call(
            'No nonempty origin value found; album origin not changed')

    def test_missing_and_empty_origin_preserve_existing_value(self):
        plugin = self.plugin()
        for data in ({}, {'Origin': ''}, {'Origin': None}):
            with self.subTest(data=data):
                (self.path / 'origin.json').write_text(json.dumps(data))
                task = self.task(origin='Existing')
                plugin.import_task_start(task, None)
                self.assertEqual(task.items[0]['origin'], 'Existing')
                self.assertNotIn('origin', plugin.tasks[task])

    def test_missing_file_does_not_add_origin(self):
        task = self.task()
        plugin = self.plugin()
        plugin.import_task_start(task, None)
        plugin.import_origin(None, task)
        self.assertNotIn('origin', task.items[0])

    def test_origin_is_stored_despite_metadata_conflict(self):
        (self.path / 'origin.json').write_text(json.dumps({
            'Origin': 'Ripper E', 'media': 'Vinyl',
        }))
        task = self.task(media='CD')
        plugin = self.plugin(patterns={'media': '$.media'})
        plugin.import_task_start(task, None)
        self.assertTrue(plugin.tasks[task]['conflict'])
        self.assertEqual(task.items[0]['media'], 'CD')
        lib = Library(str(self.path / 'library.db'), directory=str(self.path))
        self.addCleanup(lib._close)
        task.album = lib.add_album(task.items)
        with patch.object(plugin._log, 'debug') as debug:
            plugin.import_origin(SimpleNamespace(lib=lib), task)
        debug.assert_any_call('Stored origin {!r} on album {} only',
                              'Ripper E', task.album.id)
        self.assertEqual(lib.get_album(task.album.id)['origin'], 'Ripper E')
        self.assertTrue(all(item.get('origin', with_album=False) is None
                            for item in lib.items()))

    def test_origin_persists_only_on_album(self):
        (self.path / 'origin.json').write_text('{"Origin": "Ripper F"}')
        task = self.task()
        plugin = self.plugin()
        plugin.import_task_start(task, None)
        lib = Library(str(self.path / 'library.db'), directory=str(self.path))
        self.addCleanup(lib._close)
        task.album = lib.add_album(task.items)
        plugin.import_origin(SimpleNamespace(lib=lib), task)
        self.assertTrue(all(item.get('origin', with_album=False) is None
                            for item in task.items))
        self.assertTrue(all(item.get('origin', with_album=False) is None
                            for item in lib.items()))
        self.assertEqual(lib.get_album(task.album.id)['origin'], 'Ripper F')

    def test_singleton_does_not_store_origin(self):
        (self.path / 'origin.json').write_text('{"Origin": "Ripper G"}')
        task = self.task()
        task.is_album = False
        task.items = task.items[:1]
        plugin = self.plugin()
        plugin.import_task_start(task, None)
        lib = Library(str(self.path / 'library.db'), directory=str(self.path))
        self.addCleanup(lib._close)
        lib.add(task.items[0])
        plugin.import_origin(SimpleNamespace(lib=lib), task)
        self.assertNotIn('origin', task.items[0])
        self.assertNotIn('origin', lib.get_item(task.items[0].id))

    def test_import_without_autotagging(self):
        (self.path / 'origin.json').write_text('{"Origin": "Ripper H"}')
        task = self.task()
        plugin = self.plugin()
        self.assertIn(plugin.import_origin, plugin.import_stages)
        lib = Library(str(self.path / 'library.db'), directory=str(self.path))
        self.addCleanup(lib._close)
        task.album = lib.add_album(task.items)
        plugin.import_origin(SimpleNamespace(lib=lib), task)
        self.assertTrue(all(item.get('origin', with_album=False) is None
                            for item in lib.items()))
        self.assertEqual(lib.get_album(task.album.id)['origin'], 'Ripper H')


if __name__ == '__main__':
    unittest.main()
