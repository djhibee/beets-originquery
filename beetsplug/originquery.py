import confuse
import glob
import json
from jsonpath_ng import parse
import os
import re
import sys
import textwrap
import yaml
from collections import OrderedDict
from beets import config, ui
from beets.util import get_most_common_tags
from beets.plugins import BeetsPlugin
from pathlib import Path

BEETS_TO_LABEL = OrderedDict([
    ('media', 'Media'),
    ('year', 'Edition year'),
    ('country', 'Country'),
    ('label', 'Record label'),
    ('barcode', 'Barcode'),
    ('catalognum', 'Catalog number'),
    ('albumdisambig', 'Edition'),
])

# Conflicts will be reported if any of these fields don't match.
CONFLICT_FIELDS = ['barcode', 'catalognum', 'media']

# Supported metadata sources that can provide extra tags
SUPPORTED_METADATA_SOURCES = ["musicbrainz", "discogs"]


def escape_braces(string):
    return string.replace('{', '{{').replace('}', '}}')


def normalize_catno(catno):
    return catno.upper().replace(' ', '').replace('-', '')


def sanitize_value(key, value):
    if key == 'media' and value == 'WEB':
        return 'Digital Media'
    if key == 'catalognum' or key == 'label':
        return re.split('[,/]', value)[0].strip()
    if key == 'year' and value == '0':
        return ''
    return value


def highlight(text, active=True):
    if active:
        return ui.colorize('text_highlight_minor', text)
    return text


class OriginQuery(BeetsPlugin):
    def __init__(self):
        super(OriginQuery, self).__init__()

        def fail(msg):
            self.error(msg)
            self.error('Plugin disabled.')

        # Use the first available source's extra tags
        self.extra_tags = []
        self.extra_tags_source = None

        for source in SUPPORTED_METADATA_SOURCES:
            try:
                source_extra_tags = config[source]["extra_tags"].get()
                if source_extra_tags and len(source_extra_tags):
                    self.extra_tags = source_extra_tags
                    self.extra_tags_source = source
                    break
            except confuse.NotFoundError:
                # This source doesn't have extra_tags configured, skip it
                continue

        if not self.extra_tags:
            return fail(
                f"Config error: No extra tags found from supported metadata sources "
                f"({', '.join(SUPPORTED_METADATA_SOURCES)}). "
                f"At least one source must have extra_tags configured."
            )

        if not self.extra_tags:
            return fail(
                f"Config error: No extra tags found from supported metadata sources "
                f"({', '.join(SUPPORTED_METADATA_SOURCES)}). "
                f"At least one source must have extra_tags configured."
            )

        self.info(f"Using extra tags from: {self.extra_tags_source}")
        self.info(f"Available extra tags: {', '.join(self.extra_tags)}")

        config_patterns = None
        try:
            config_patterns = self.config['tag_patterns'].get()
            if not isinstance(config_patterns, dict):
                raise confuse.ConfigError()
        except confuse.ConfigError:
            return fail('Config error: originquery.tag_patterns must be set to a dictionary of key -> pattern mappings.')

        try:
            self.origin_file = Path(self.config['origin_file'].get())
        except confuse.NotFoundError:
            return fail('Config error: originquery.origin_file not set.')
        self.tag_patterns = {}

        try:
            origin_type = self.config['origin_type'].as_choice(['yaml', 'json', 'text']).lower()
        except confuse.NotFoundError:
            origin_type = self.origin_file.suffix.lower()[1:]

        if origin_type == 'json':
            self.match_fn = self.match_json
        elif origin_type == 'yaml':
            self.match_fn = self.match_yaml
        else:
            self.match_fn = self.match_text

        for key, pattern in config_patterns.items():
            if key not in BEETS_TO_LABEL:
                return fail(f'Config error: unknown key "{key}"')
                self.error("Plugin disabled.")

            if origin_type == 'json' or origin_type == 'yaml':
                try:
                    self.tag_patterns[key] = parse(pattern)
                except Exception as e:
                    return fail(
                        f'Config error: invalid tag pattern for "{key}". '
                        f'"{pattern}" is not a valid JSON path ({format(str(e))}).'
                    )
                continue

            try:
                regex = re.compile(pattern)
                self.tag_patterns[key] = regex
            except re.error as e:
                return fail(
                    f'Config error: invalid tag pattern for "{key}". '
                    f'"{pattern}" is not a valid regex ({format(str(e))}).'
                )
            if regex.groups != 1:
                return fail(
                    f'Config error: invalid tag pattern for "{key}". '
                    f'"{pattern}" must have exactly one capture group.'
                )

        self.register_listener('import_task_start', self.import_task_start)
        # import_task_start fires early in beets' pipeline, which searches
        # several albums ahead of whichever one the user is actually being
        # prompted for -- printing the origin-data table there means it
        # shows up for releases well before you review them. Do the actual
        # data work (below) in import_task_start, since it must land before
        # the MusicBrainz search happens, but defer the printing to
        # before_choose_candidate, which fires synchronously right as this
        # task's own prompt is being built.
        self.register_listener('before_choose_candidate', self.before_choose_candidate)
        # beet move/modify -m relocates only the files beets actually
        # tracks as library items -- a plain move leaves origin.yaml
        # behind in the old directory, since that file was only ever
        # copied in at import time (_copy_origin_file above), not
        # tracked. Carry it along whenever an already-imported album
        # gets moved later.
        self.register_listener('item_moved', self.item_moved)
        self.tasks = {}

        try:
            self.use_origin_on_conflict = self.config['use_origin_on_conflict'].get(bool)
        except confuse.NotFoundError:
            self.use_origin_on_conflict = False


    def error(self, msg):
        self._log.error(escape_braces(ui.colorize('text_error', msg)))


    def warn(self, msg):
        self._log.warning(escape_braces(ui.colorize('text_warning', msg)))


    def info(self, msg):
        # beets defaults to log level warning for event handlers.
        self._log.warning(escape_braces(msg))


    def print_tags(self, items, use_tagged):
        headers = ['Field', 'Tagged Data', 'Origin Data']

        w_key = max(len(headers[0]), *(len(BEETS_TO_LABEL[k]) for k, v in items))
        natural_tagged = max(len(headers[1]), *(len(v['tagged']) for k, v in items))
        natural_origin = max(len(headers[2]), *(len(v['origin']) for k, v in items))

        # Cap each data column to what actually fits the terminal instead
        # of always sizing to the longest value (e.g. a long Genres list)
        # -- an uncapped table wider than the terminal gets raw-wrapped by
        # the terminal itself mid-line, breaking the box-drawing border
        # rather than staying inside it. "║ " + key + " │ " + tagged +
        # " │ " + origin + " ║" is 10 characters of fixed overhead beyond
        # the three column widths. beets' console formatter also prepends
        # "{plugin name}: " (LegacyFormatter, beets/logging.py) to every
        # line logged via self.info() *after* this method returns its
        # already-wrapped lines -- that prefix isn't part of the string
        # being measured here, but it still eats into the terminal's real
        # width once printed, so it has to be budgeted for too or long
        # cells wrap again at the terminal level, mid-word, outside the
        # box border.
        term_width = shutil.get_terminal_size(fallback=(80, 24)).columns
        prefix_width = len(self.name) + 2
        available = max(term_width - prefix_width - w_key - 10, 20)
        max_data_col = max(available // 2, 10)
        w_tagged = min(natural_tagged, max_data_col)
        w_origin = min(natural_origin, max_data_col)

        def wrap_cell(text, width):
            return textwrap.wrap(text, width) or ['']

        self.info('╔{0}╤{1}╤{2}╗'.format('═' * (w_key + 2), '═' * (w_tagged + 2), '═' * (w_origin + 2)))
        self.info('║ {0} │ {1} │ {2} ║'.format(headers[0].ljust(w_key),
                                               highlight(headers[1].ljust(w_tagged), use_tagged),
                                               highlight(headers[2].ljust(w_origin), not use_tagged)))
        self.info('╟{0}┼{1}┼{2}╢'.format('─' * (w_key + 2), '─' * (w_tagged + 2), '─' * (w_origin + 2)))
        for k, v in items:
            if not v['tagged'] and not v['origin']:
                continue
            tagged_active = use_tagged and v['active']
            origin_active = not use_tagged and v['active']
            tagged_lines = wrap_cell(v['tagged'], w_tagged)
            origin_lines = wrap_cell(v['origin'], w_origin)
            for i in range(max(len(tagged_lines), len(origin_lines))):
                key_text = BEETS_TO_LABEL[k] if i == 0 else ''
                tagged_text = tagged_lines[i] if i < len(tagged_lines) else ''
                origin_text = origin_lines[i] if i < len(origin_lines) else ''
                self.info('║ {0} │ {1} │ {2} ║'.format(
                    key_text.ljust(w_key),
                    highlight(tagged_text.ljust(w_tagged), tagged_active),
                    highlight(origin_text.ljust(w_origin), origin_active)))
        self.info('╚{0}╧{1}╧{2}╝'.format('═' * (w_key + 2), '═' * (w_tagged + 2), '═' * (w_origin + 2)))


    def match_text(self, origin_path):
        with open(origin_path, encoding="utf-8") as f:
            lines = f.readlines()

        for key, pattern in self.tag_patterns.items():
            for line in lines:
                line = line.strip()
                match = re.match(pattern, line)
                if not match:
                    continue
                yield key, match[1]


    def match_json(self, origin_path):
        with open(origin_path, encoding="utf-8") as f:
            data = json.load(f)

        for key, pattern in self.tag_patterns.items():
            match = pattern.find(data)
            if not len(match):
                continue

            yield key, str(match[0].value)


    def match_yaml(self, origin_path):
        with open(origin_path, encoding="utf-8") as f:
            data = yaml.load(f, Loader=yaml.SafeLoader)

        for key, pattern in self.tag_patterns.items():
            match = pattern.find(data)
            if not len(match) or not match[0].value:
                continue
            yield key, str(match[0].value)


    def import_task_start(self, task, session):
        task_info = self.tasks[task] = {}

        # In case this is a multi-disc import, find the common parent directory.
        base = os.path.commonpath(task.paths).decode('utf8')

        glob_pattern = os.path.join(glob.escape(base), self.origin_file)
        origin_glob = sorted(glob.glob(glob_pattern))
        if len(origin_glob) < 1:
            task_info['origin_path'] = Path(base) / self.origin_file
            task_info['missing_origin'] = True
            self.warn('No origin file found at {0}'.format(task_info['origin_path']))
            return
        task_info['origin_path'] = origin_path = Path(origin_glob[0])

        conflict = False
        likelies = get_most_common_tags(task.items)
        task_info['tag_compare'] = tag_compare = OrderedDict()
        for tag in BEETS_TO_LABEL:
            tag_compare.update({tag: {
                'tagged': str(likelies[tag]),
                'active': tag in self.extra_tags,
                'origin': '',
            }})

        for key, value in self.match_fn(origin_path):
            if tag_compare[key]['origin']:
                continue

            tagged_value = tag_compare[key]['tagged']
            origin_value = sanitize_value(key, value)
            tag_compare[key]['origin'] = origin_value
            if key not in CONFLICT_FIELDS or not tagged_value or not origin_value:
                continue

            if key == 'catalognum':
                tagged_value = normalize_catno(tagged_value)
                origin_value = normalize_catno(origin_value)

            if tagged_value != origin_value:
                conflict = task_info['conflict'] = True

        if not conflict or self.use_origin_on_conflict:
            # Update all item with origin metadata.
            for item in task.items:
                for tag, entry in tag_compare.items():
                    origin_value = entry['origin']
                    if tag not in self.extra_tags:
                        continue
                    if tag == 'year' and origin_value:
                        origin_value = int(origin_value) if origin_value.isdigit() else ''
                    item[tag] = origin_value

                # beets weighs media heavily, and will even prioritize a media match over an exact catalognum match.
                # At the same time, media for uploaded music is often mislabeled (e.g., Enhanced CD and SACD are just
                # grouped as CD). This does not make a good combination. As a workaround, lower the weight for media
                # if we also have a catalognum.
                if item['media'] and item['catalognum']:
                    config['match']['distance_weights']['media'] = .2

    def before_choose_candidate(self, session, task):
        task_info = self.tasks.get(task)
        if not task_info or task_info.get('missing_origin'):
            return
        self.info('Using origin file {0}'.format(task_info['origin_path']))
        conflict = task_info.get('conflict')
        use_tagged = conflict and not self.use_origin_on_conflict
        self.print_tags(task_info.get('tag_compare').items(), use_tagged)
        if conflict:
            self.warn("Origin data conflicts with tagged data.")

     def item_moved(self, item, source, destination):
        source_dir = os.path.dirname(source).decode('utf8')
        dest_dir = os.path.dirname(destination).decode('utf8')
        if source_dir == dest_dir:
            return
        glob_pattern = os.path.join(glob.escape(source_dir), self.origin_file)
        matches = glob.glob(glob_pattern)
        if not matches:
            return
        origin_path = matches[0]
        dest_path = os.path.join(dest_dir, os.path.basename(origin_path))
        if os.path.exists(dest_path):
            return  # already carried along by an earlier item in this album
        try:
            shutil.move(origin_path, dest_path)
        except OSError as exc:
            self.warn('Could not carry origin file to new location: {0}'.format(exc))
