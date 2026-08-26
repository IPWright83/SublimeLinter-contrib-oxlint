# SublimeLinter-oxlint

This linter plugin for [SublimeLinter](https://github.com/SublimeLinter/SublimeLinter) provides an interface to [oxlint](https://oxc.rs/docs/guide/usage/linter), the Rust-based JavaScript and TypeScript linter from [oxc](https://oxc.rs).

It also drives oxlint's fixer and, optionally, [oxfmt](https://oxc.rs/docs/guide/usage/formatter) for formatting — on demand or on save.

## Installation

SublimeLinter must be installed in order to use this plugin.

Please install via [Package Control](https://packagecontrol.io).

### Linter installation

`oxlint` must be installed before using this plugin. Adding it as a dev dependency is recommended, and is detected automatically:

```sh
npm install --save-dev oxlint
```

The plugin looks for `node_modules/.bin/oxlint` starting from the linted file and walking up towards `$HOME`. In a Yarn project it runs `yarn run --silent oxlint` instead. Failing that, it falls back to an `oxlint` on your PATH (`npm install -g oxlint`, `brew install oxlint`, …).

**Note:** GUI applications on macOS and Linux don't inherit the PATH from your shell, so a globally installed `oxlint` may not be found. See [Debugging PATH problems](https://sublimelinter.readthedocs.io/en/latest/troubleshooting.html#debugging-path-problems).

### Formatting installation

[oxfmt](https://oxc.rs/docs/guide/usage/formatter) is oxc's formatter. If you wish to include formatting install it alongside oxlint:

```sh
npm install --save-dev oxfmt
```

## Behaviour

### Linting happens on save

`oxlint` has no stdin mode, and it resolves `.oxlintrc.json` and `tsconfig.json` relative to the file being linted. The plugin therefore lints the real file on disk (`tempfile_suffix = '-'`), which means diagnostics update when you save, not as you type. Unsaved buffers are not linted.

### Configuration

`oxlint` finds its own configuration — `.oxlintrc.json` (including [nested configs](https://oxc.rs/docs/guide/usage/linter/nested-config)) and the relevant `tsconfig.json`. There is nothing to configure in the plugin for that.

## Settings

See SublimeLinter's [settings docs](https://sublimelinter.readthedocs.io/en/latest/settings.html) for how and where to set these. Common ones:

```json
{
    "linters": {
        "oxlint": {
            "args": ["--type-aware"],
            "disable_if_not_dependency": true
        }
    }
}
```

| Setting | Description |
| --- | --- |
| `args` | Extra arguments passed to `oxlint`, e.g. `["-D", "correctness"]`, `["--react-plugin"]`, `["--type-aware"]`. |
| `disable_if_not_dependency` | Only lint when `oxlint` is a dependency of the project. Useful if you also run `SublimeLinter-eslint`, so that each only reports where it belongs. |
| `executable` | Point at a specific `oxlint` binary. Disables the `node_modules` lookup. |
| `fix_on_save` | Which fixes to apply after saving: `"off"`, `"safe"`, `"suggestions"` or `"dangerously"`. See [Fix on save](#fix-on-save). |
| `format_on_save` | Run `oxfmt` after saving. See [Formatting with oxfmt](#formatting-with-oxfmt). |
| `selector` | Which files to lint. Defaults to JS/JSX/TS/TSX plus Vue, Astro and Svelte. |

Do not pass `--fix` in `args`: `oxlint` would rewrite the file behind Sublime Text's back, and fixed problems are omitted from its output. Use the [Oxlint: Fix This File](#auto-fix-oxlint---fix) command instead.

## Fixing problems

SublimeLinter has no "run the linter's fixer" feature of its own, so this plugin provides both halves itself.

### Quick actions (silence a rule)

Like the other SublimeLinter plugins, the quick action inserts an `// oxlint-disable-next-line <rule>` comment above the offending line, merging into an existing comment where there is one. Put the caret on the error (or select several lines) and run **SublimeLinter: Quick Action** from the command palette, or bind it:

```json
{ "keys": ["super+alt+a"], "command": "sublime_linter_quick_actions" }
```

### Auto-fix (`oxlint --fix`)

Run **Oxlint: Fix This File** from the command palette. `oxlint` rewrites the file on disk, so the command saves the view, runs the fixer, reads the result back into the buffer as a single undoable edit, saves, and re-lints. Bind it with:

```json
{ "keys": ["super+alt+f"], "command": "oxlint_fix" }
```

A *fix* is safe: it preserves behaviour, like `no-var` rewriting `var` to `const`. A *suggestion* may change behaviour, so oxlint will not apply it unless asked — `no-console` deletes the statement, for example.

oxlint's fix flags are mutually exclusive — passing more than one is an error — and `--fix-suggestions` applies suggestions *instead of* safe fixes rather than as well. To apply both, this plugin runs oxlint twice, which composes because each pass writes the file:

| Command palette entry | `mode` | Passes | Safe fixes | Suggestions |
| --- | --- | --- | --- | --- |
| Oxlint: Fix This File | `safe` | `--fix` | yes | no |
| Oxlint: Fix This File (with suggestions) | `suggestions` | `--fix`, then `--fix-suggestions` | yes | yes |
| Oxlint: Fix This File (dangerously) | `dangerously` | `--fix-dangerously` | yes | yes, plus dangerous ones |

Given `var a = 1; console.log(a);`, those three produce respectively:

```js
const a = 1;  console.log(a);   // safe
const a = 1;                    // suggestions
const a = 1;                    // dangerously
```

Pass `{"mode": "..."}` as the command's args to bind any of them to a key.

The fixer runs in the same environment as the lint: both the global `paths` setting and the `oxlint` linter's own `env` setting are applied, which is what makes the `#!/usr/bin/env node` shim in `node_modules/.bin` work in a GUI application that didn't inherit your shell's PATH. Unlike SublimeLinter, an `env.PATH` is *prepended* to the inherited PATH rather than replacing it, so pointing it at just a node install is enough:

```json
{
    "linters": {
        "oxlint": {
            "env": { "PATH": "/Users/you/.nvm/versions/node/v24.14.0/bin" }
        }
    }
}
```

Only rules that are actually enabled get fixed, so if nothing happens, check that the rule is on in your `.oxlintrc.json` — and note that many rules have no fix at all.

### Fix on save

Set `fix_on_save` in the `oxlint` linter settings to the fixes you want applied after every save:

```json
{
    "linters": {
        "oxlint": {
            "fix_on_save": "safe"
        }
    }
}
```

| Value | Effect |
| --- | --- |
| `"off"` (default) | Never fix on save. |
| `"safe"` | Safe fixes only. |
| `"suggestions"` | Safe fixes and suggestions. May change what your code does. |
| `"dangerously"` | Everything, dangerous fixes included. Rarely what you want on a keystroke. |

`true` and `false` are accepted as aliases for `"safe"` and `"off"`, as is `"fix"` after oxlint's own flag name. Anything else is ignored, with a note in the console.

Only buffers SublimeLinter actually lints with oxlint are touched, so the setting respects `selector` and `disable_if_not_dependency` without repeating them.

## Formatting with oxfmt

[oxfmt](https://oxc.rs/docs/guide/usage/formatter) is oxc's formatter. Install it alongside oxlint if you want it:

```sh
npm install --save-dev oxfmt
```

Run **Oxfmt: Format This File** from the command palette, or bind it:

```json
{ "keys": ["super+alt+p"], "command": "oxfmt_format" }
```

`oxfmt` formats standard input, so the command works on an unsaved buffer and does not save afterwards — it only reformats what you are looking at. Your `.oxfmtrc.json` is still found, from the buffer's path.

To format on every save, set `format_on_save`:

```json
{
    "linters": {
        "oxlint": {
            "fix_on_save": "safe",
            "format_on_save": true
        }
    }
}
```

With both set, fixing runs first — fixes can leave code that wants reformatting, never the other way round — and the two share a single save and a single re-lint. Like `fix_on_save`, this only touches buffers SublimeLinter lints with oxlint, so oxlint needs to be set up even if it is oxfmt you care about. The command itself needs only oxfmt.

## License

MIT — see [LICENSE](LICENSE).
