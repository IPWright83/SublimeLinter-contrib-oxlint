#
# fix.py
# `oxlint --fix` and `oxfmt` as Sublime Text commands.
#
# License: MIT
#

"""This module exports the `oxlint_fix` and `oxfmt_format` commands.

SublimeLinter runs linters, not fixers or formatters, so these are plain
Sublime Text commands.  `oxlint` fixes files on disk, so we save the view, run
it, and read the result back into the buffer; `oxfmt` can format standard
input, so it needs no file at all.

Note: apart from asking SublimeLinter which linters it assigned to a buffer,
this module imports nothing from it.  Sublime Text may load us in a different
plugin host than SublimeLinter (and hence `linter.py`) runs in, and modules are
not shared between hosts, so we otherwise talk to it by command name only.
"""

import logging
import os
import shutil
import subprocess
import sys
import threading

import sublime
import sublime_plugin

try:
    # Only used to tell whether SublimeLinter lints this buffer with oxlint.
    # If we are in a foreign plugin host these imports fail, and the on-save
    # handlers stay off; the commands themselves never depend on them.
    from SublimeLinter.lint import elect, persist
except ImportError:  # pragma: no cover
    elect = persist = None

logger = logging.getLogger('SublimeLinter.plugin.oxlint')

# Set to False to quieten the console.  `logger` output is unreliable here:
# SublimeLinter configures logging in *its* plugin host, which may not be ours.
DEBUG = True

TIMEOUT = 30

# Buffer ids we are currently saving ourselves.
_saving = set()

# Tools we have already complained about not finding.
_missing = set()

# oxlint's fix flags are mutually exclusive -- passing more than one is an
# error -- and each selects a set of fixes rather than adding to the previous
# one: `--fix-suggestions` applies suggestions *instead of* safe fixes.  To get
# both we run oxlint twice, which composes because each pass writes the file.
#
# So a mode is a sequence of passes:
#
#   safe         safe fixes only
#   suggestions  safe fixes, then suggestions (may change behaviour)
#   dangerously  everything, in a single pass that already includes both
#
MODES = {
    'safe': ('--fix',),
    'suggestions': ('--fix', '--fix-suggestions'),
    'dangerously': ('--fix-dangerously',),
}


def debug(message, *args):
    text = message % args if args else message
    logger.info(text)
    if DEBUG:
        print('[oxlint]', text)


debug(
    'fix.py loaded (python %s, sublime build %s)',
    sys.version.split()[0],
    sublime.version(),
)


# --- settings ---------------------------------------------------------------

def linter_settings():
    """Read the `oxlint` linter settings SublimeLinter would use."""
    settings = sublime.load_settings('SublimeLinter.sublime-settings')
    linters = settings.get('linters') or {}
    return linters.get('oxlint') or {}, settings.get('paths') or {}


def fix_on_save_mode():
    """Which fixes, if any, the `fix_on_save` setting asks for on save.

    One of `MODES` -- usually 'safe' or 'suggestions' -- or None for 'off'.
    Booleans are accepted too, since they are an easy thing to write.
    """
    oxlint_settings, _ = linter_settings()
    value = oxlint_settings.get('fix_on_save', 'off')

    if value is True or value == 'fix':  # 'fix' after oxlint's own flag name
        return 'safe'
    if not value or value == 'off':
        return None
    if value in MODES:
        return value

    debug('fix_on_save: %r is not a mode, expected one of: off, %s',
          value, ', '.join(MODES))
    return None


def format_on_save_enabled():
    """Is `format_on_save` turned on in the `oxlint` linter settings?"""
    oxlint_settings, _ = linter_settings()
    return bool(oxlint_settings.get('format_on_save', False))


def lints_this_buffer(view):
    """Does SublimeLinter lint this buffer with oxlint?

    Asks `elect` which linters apply to the view right now.  Its
    `assigned_linters` registry would be simpler, but that is only filled in
    once a lint has actually completed for the buffer, so on the first save of
    a session -- or any save that beats the lint -- it is empty and we would
    wrongly skip.
    """
    if elect is not None:
        try:
            names = {
                info.name
                for info in elect.assignable_linters_for_view(view, 'on_save')
            }
        except Exception as err:
            # A private API, so never let it break saving.
            debug('could not ask SublimeLinter which linters apply: %s', err)
        else:
            if 'oxlint' in names:
                return True

            debug('oxlint does not apply to this view; SublimeLinter offers %s'
                  ' and knows about %s',
                  ', '.join(sorted(names)) or 'no linters here',
                  ', '.join(sorted(persist.linter_classes)) if persist else '?')
            return False

    if persist is not None:
        return 'oxlint' in persist.assigned_linters.get(view.buffer_id(), set())

    debug('SublimeLinter is in another plugin host; on-save handlers are off')
    return False


def create_environment():
    """Build the environment SublimeLinter lints with.

    The `oxlint` and `oxfmt` files in `node_modules/.bin` are
    `#!/usr/bin/env node` shims, and GUI applications don't inherit the shell's
    PATH, so node is often not findable without help.  SublimeLinter takes that
    help from its own `paths` setting and the linter's `env` setting, so we
    honour both here.  Unlike SublimeLinter we prepend to PATH rather than
    replacing it, so that pointing `env.PATH` at just a node install keeps the
    rest of the system reachable.
    """
    oxlint_settings, paths = linter_settings()

    env = os.environ.copy()
    extra_paths = [
        os.path.expanduser(path)
        for path in paths.get(sublime.platform(), [])
    ]

    for key, value in (oxlint_settings.get('env') or {}).items():
        if key.upper() == 'PATH':
            extra_paths = [
                os.path.expanduser(path) for path in value.split(os.pathsep)
            ] + extra_paths
        else:
            env[key] = value

    if extra_paths:
        env['PATH'] = os.pathsep.join(extra_paths + [env.get('PATH', '')])

    return env


# --- running the tools ------------------------------------------------------

def report_missing(name, start_dir, env):
    """Say a tool could not be found, once per tool.

    `format_on_save` runs on every save, and a missing `oxfmt` is a perfectly
    ordinary state -- it is an optional extra -- so this must not become noise.
    """
    if name in _missing:
        return

    _missing.add(name)
    status('{}: executable not found'.format(name))
    debug('could not find %s starting at %s; PATH=%s',
          name, start_dir, env.get('PATH'))


def find_executable(name, start_dir, env=None):
    """Find `name` in a local `node_modules/.bin`, else on the PATH.

    Returns `(None, None)` when it is nowhere to be found; callers treat that
    as "skip this tool", never as an error.
    """
    search_path = (env or os.environ).get('PATH', '')

    path = start_dir
    while True:
        executable = shutil.which(name, path=os.path.join(path, 'node_modules', '.bin'))
        if executable:
            return executable, path

        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent

    executable = shutil.which(name, path=search_path)
    if executable:
        debug('no local %s below %s, falling back to PATH', name, start_dir)
        _missing.discard(name)
        return executable, start_dir

    return None, None


def run_tool(cmd, cwd, env, stdin=None):
    """Run a tool, returning its `CompletedProcess` or None if it never ran."""
    debug('running %s (cwd=%s)', cmd, cwd)
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            input=stdin.encode('utf-8') if stdin is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=create_startupinfo(),
            timeout=TIMEOUT,
        )
    except OSError as err:
        status('oxlint: {}'.format(err))
        debug('running %s failed: %s', cmd[0], err)
        return None
    except subprocess.TimeoutExpired:
        status('oxlint: timed out')
        debug('%s timed out after %ss', cmd[0], TIMEOUT)
        return None

    stderr = proc.stderr.decode('utf-8', 'replace')
    debug('exit=%s stderr=%r', proc.returncode, stderr[:500])
    return proc


def oxlint_fix(filename, mode, env):
    """Fix `filename` in place, one pass per flag.  True if oxlint ran."""
    start_dir = os.path.dirname(filename)
    executable, project_root = find_executable('oxlint', start_dir, env)
    if not executable:
        report_missing('oxlint', start_dir, env)
        return False

    ran = False
    for flag in MODES[mode]:
        cmd = [executable, flag, '--silent', '--', filename]
        # oxlint exits non-zero when problems remain, not a failure here.
        ran = run_tool(cmd, project_root, env) is not None or ran

    return ran


def oxfmt_format(text, filename, env):
    """Format `text` as `filename` would be.  Returns the formatted text.

    Returns None if oxfmt is not installed at all, could not run, or refused
    -- on a parse error it writes to stderr and produces nothing, and for an
    ignored file it echoes its input back unchanged.
    """
    start_dir = os.path.dirname(filename)
    executable, project_root = find_executable('oxfmt', start_dir, env)
    if not executable:
        report_missing('oxfmt', start_dir, env)
        return None

    cmd = [executable, '--stdin-filepath={}'.format(filename)]
    proc = run_tool(cmd, project_root, env, stdin=text)
    if proc is None:
        return None

    if proc.returncode != 0 or not proc.stdout:
        status('oxfmt: could not format this file')
        return None

    return proc.stdout.decode('utf-8')


# --- applying the result to the view ----------------------------------------

def status(message):
    sublime.set_timeout(lambda: sublime.status_message(message))


def apply_to_view(view, filename, text, save):
    """Put `text` in the view, keeping the cursors, and optionally save."""
    if view.file_name() != filename:  # the user moved on
        debug('view now shows %s, not touching %s', view.file_name(), filename)
        return

    if text == view.substr(sublime.Region(0, view.size())):
        debug('nothing changed')
        sublime.status_message('oxlint: nothing to do')
        return

    cursors = [(region.a, region.b) for region in view.sel()]
    viewport = view.viewport_position()

    debug('replacing buffer (%s chars -> %s chars)', view.size(), len(text))
    view.run_command('oxlint_replace_buffer', {'text': text})

    view.sel().clear()
    size = view.size()
    view.sel().add_all([
        sublime.Region(min(a, size), min(b, size)) for a, b in cursors
    ])
    view.set_viewport_position(viewport, False)

    if save:
        _saving.add(view.buffer_id())
        try:
            view.run_command('save')
        finally:
            _saving.discard(view.buffer_id())

        window = view.window()
        if window:
            window.run_command('sublime_linter_lint')

    debug('done')


def read_file(filename):
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return f.read()
    except OSError as err:
        debug('could not read %s: %s', filename, err)
        return None


def create_startupinfo():
    if sublime.platform() != 'windows':
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return info


# --- commands ---------------------------------------------------------------

class OxlintFixCommand(sublime_plugin.TextCommand):
    """Run oxlint's fixer against this file and reload the buffer.

    Args:
        mode: which fixes to apply, one of `MODES`.  Defaults to safe fixes.
    """

    def is_enabled(self):
        return bool(self.view.file_name())

    def run(self, edit, mode='safe'):
        filename = self.view.file_name()
        debug('oxlint_fix: file=%s mode=%s', filename, mode)
        if not filename:
            debug('oxlint_fix: buffer has no filename, nothing to do')
            return

        if mode not in MODES:
            debug('oxlint_fix: unknown mode %r, expected one of %s',
                  mode, ', '.join(sorted(MODES)))
            return

        if self.view.is_dirty():
            debug('saving dirty view first')
            self.view.run_command('save')

        run_in_background(
            fix_and_format, self.view, filename, mode, False, create_environment())


class OxfmtFormatCommand(sublime_plugin.TextCommand):
    """Format this buffer with oxfmt.

    oxfmt reads standard input, so this works on an unsaved buffer, and does
    not save afterwards -- it only formats what you are looking at.
    """

    def run(self, edit):
        view = self.view
        filename = view.file_name() or fallback_filename(view)
        debug('oxfmt_format: file=%s', filename)

        text = view.substr(sublime.Region(0, view.size()))
        env = create_environment()

        def worker():
            formatted = oxfmt_format(text, filename, env)
            if formatted is not None:
                sublime.set_timeout(
                    lambda: apply_to_view(view, view.file_name(), formatted, False))

        threading.Thread(target=worker).start()


SYNTAX_EXTENSIONS = {
    'JavaScript': 'js',
    'JSX': 'jsx',
    'TypeScript': 'ts',
    'TSX': 'tsx',
}


def fallback_filename(view):
    """A filename for an unsaved buffer, so oxfmt can pick a parser.

    The directory matters as much as the extension: it is what oxfmt resolves
    `.oxfmtrc.json` from, so use the window's first folder where there is one.
    """
    syntax = os.path.splitext(os.path.basename(view.settings().get('syntax') or ''))[0]
    window = view.window()
    folders = window.folders() if window else []
    return os.path.join(
        folders[0] if folders else os.path.expanduser('~'),
        '__buffer__.{}'.format(SYNTAX_EXTENSIONS.get(syntax, 'js')),
    )


def run_in_background(fn, *args):
    threading.Thread(target=fn, args=args).start()


def fix_and_format(view, filename, mode, do_format, env):
    """Fix and/or format `filename`, then put the result in the view.

    Runs in a worker thread: both tools are subprocesses, and the view is only
    touched back on the UI thread.
    """
    if mode:
        oxlint_fix(filename, mode, env)

    text = read_file(filename)
    if text is None:
        return

    if do_format:
        formatted = oxfmt_format(text, filename, env)
        if formatted is not None:
            text = formatted

    sublime.set_timeout(lambda: apply_to_view(view, filename, text, True))


class OxlintReplaceBufferCommand(sublime_plugin.TextCommand):
    """Replace the whole buffer in a single, undoable edit."""

    def run(self, edit, text):
        view = self.view
        read_only = view.is_read_only()
        view.set_read_only(False)
        try:
            view.replace(edit, sublime.Region(0, view.size()), text)
        finally:
            view.set_read_only(read_only)


class OxlintOnSave(sublime_plugin.EventListener):
    """Fix and format after saving, as asked for by the linter settings.

    Only for buffers SublimeLinter actually lints with oxlint.  Fixing runs
    first: fixes can leave code that wants reformatting, never the other way
    round.  Both share a single save and a single re-lint.
    """

    def on_post_save_async(self, view):
        if view.buffer_id() in _saving:  # this save was ours
            return

        mode = fix_on_save_mode()
        do_format = format_on_save_enabled()
        if not mode and not do_format:
            return

        filename = view.file_name()
        if not filename:
            return

        if not lints_this_buffer(view):
            debug('on_save: oxlint is not assigned to %s, skipping', filename)
            return

        debug('on_save: %s (fix=%s, format=%s)', filename, mode, do_format)
        run_in_background(
            fix_and_format, view, filename, mode, do_format, create_environment())
