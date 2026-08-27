#
# linter.py
# Linter for SublimeLinter, a code checking framework for Sublime Text
#
# License: MIT
#

"""This module exports the Oxlint plugin class."""

import json
import logging
import re

from SublimeLinter.lint import LintMatch, NodeLinter
from SublimeLinter.lint.quick_fix import (
    ignore_rules_inline,
    extend_existing_comment,
    insert_preceding_line,
    line_error_is_on,
    read_previous_line,
)

logger = logging.getLogger('SublimeLinter.plugin.oxlint')

# oxlint reports the rule as e.g. "eslint(no-debugger)" or
# "typescript(no-explicit-any)".  We surface just the rule name as the `code`.
CODE_RE = re.compile(r'^(?P<plugin>[^(]+)\((?P<rule>.+)\)$')

# Note: `fix.py` duplicates the default selector these build, because Sublime
# Text may load it in another plugin host where it cannot import this module.
# Keep `DEFAULT_SELECTOR` there in sync with these two.
STANDARD_SELECTOR = (
    'source.js, source.jsx, source.mjs, source.cjs, '
    'source.ts, source.tsx, source.mts, source.cts'
)
PLUGIN_SELECTOR = 'text.html.vue, source.astro, source.svelte'


class Oxlint(NodeLinter):
    cmd = ('oxlint', '--format=json', '${args}', '--', '${file}')

    # oxlint has no stdin mode, and it resolves `.oxlintrc.json` and
    # `tsconfig.json` relative to the file it lints, so a temp file elsewhere
    # on disk would be linted with the wrong configuration.  Lint the real
    # file instead, which means: on save only.
    tempfile_suffix = '-'

    defaults = {
        'selector': '{}, {}'.format(STANDARD_SELECTOR, PLUGIN_SELECTOR),
        # Which fixes to apply after saving: 'off', 'safe', 'suggestions'
        # or 'dangerously'.
        # Implemented in `fix.py`; declared here so it is documented
        # alongside the other linter settings.
        'fix_on_save': 'off',
        # Run `oxfmt` after saving.  Also implemented in `fix.py`.
        'format_on_save': False,
    }

    def find_errors(self, output):
        if not output.strip():
            return

        try:
            report = json.loads(output)
        except ValueError:
            logger.error('Could not parse oxlint output:\n%s', output[:2000])
            self.notify_failure()
            return

        for diagnostic in report.get('diagnostics', []):
            match = self.parse_diagnostic(diagnostic)
            if match:
                yield match

    def parse_diagnostic(self, diagnostic):
        labels = diagnostic.get('labels') or []
        span = (labels[0].get('span') if labels else None) or {}
        line, col = span.get('line'), span.get('column')
        if line is None or col is None:
            logger.warning('Skipping diagnostic without a position: %s', diagnostic)
            return None

        code = diagnostic.get('code') or ''
        match = CODE_RE.match(code)
        rule = match.group('rule') if match else code

        message = diagnostic.get('message', '')
        help_text = diagnostic.get('help')
        if help_text:
            message = '{} ({})'.format(message, help_text)

        # Note: a custom `find_errors` bypasses `split_match`, so we have to
        # apply `line_col_base` ourselves; oxlint counts from 1.
        #
        # We only ever lint the current file, and oxlint reports paths relative
        # to its working dir, so don't set `filename` at all.
        return LintMatch(
            line=self.apply_line_base(line),
            col=self.apply_col_base(col),
            # SublimeLinter clamps `end_col` to the end of the line, so a
            # multi-line span simply highlights up to the end of its first line.
            end_line=self.apply_line_base(line),
            end_col=self.apply_col_base(col + (span.get('length') or 0)),
            message=message,
            error_type='error' if diagnostic.get('severity') == 'error' else 'warning',
            code=rule,
        )


@ignore_rules_inline('oxlint')
def fix_oxlint_error(error, view):
    """Provide a quick action to silence a rule for this line."""
    line = line_error_is_on(view, error)
    code = error['code']
    yield (
        extend_existing_comment(
            r'// oxlint-disable-next-line (?P<codes>[\w\-/]+(?:,\s?[\w\-/]+)*)'
            r'(?P<comment>\s+-{2,})?',
            ', ',
            {code},
            read_previous_line(view, line)
        )
        or insert_preceding_line(
            '// oxlint-disable-next-line {}'.format(code),
            line
        )
    )
