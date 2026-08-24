# Peregrine syntax highlighting

`peregrine.tmLanguage.json` is a [TextMate grammar](https://macromates.com/manual/en/language_grammars)
for Peregrine (`.pgr` files) — the format VS Code, Sublime Text, and most other editors with a
TextMate-compatible tokenizer consume for syntax highlighting. It covers keywords (`let fn if else
true false return`), operators, string literals with `\n`/`\t`/`\"`/`\\` escapes, `//` line
comments, integer/float literals, function names (both declarations and call sites), and general
identifiers.

This directory intentionally contains only the grammar file itself — wiring it into a full VS Code
extension (a `package.json` with a `contributes.languages`/`contributes.grammars` block, an
extension manifest, packaging/publishing) is out of scope here. What follows is how to point an
editor at the grammar directly without any of that.

## VS Code (minimal, unpublished extension)

VS Code loads grammars only from an installed extension, so a minimal one is unavoidable — but it's
a handful of files, not a real extension:

1. Create a folder under your VS Code extensions directory, e.g.
   `~/.vscode/extensions/peregrine-syntax/`.
2. Copy `peregrine.tmLanguage.json` into it.
3. Add a `package.json`:

   ```json
   {
     "name": "peregrine-syntax",
     "version": "0.0.1",
     "engines": { "vscode": "^1.50.0" },
     "contributes": {
       "languages": [{ "id": "peregrine", "extensions": [".pgr"], "aliases": ["Peregrine"] }],
       "grammars": [
         { "language": "peregrine", "scopeName": "source.peregrine", "path": "./peregrine.tmLanguage.json" }
       ]
     }
   }
   ```

4. Restart VS Code (or reload the window). `.pgr` files will now highlight using this grammar.

## Sublime Text / other TextMate-grammar consumers

Most editors that accept `.tmLanguage`/`.tmLanguage.json` grammars directly (rather than requiring
a full extension package, as VS Code does) can simply be pointed at
`peregrine.tmLanguage.json` — check your editor's own docs for where it expects
user-supplied grammars to live.

## Scope naming reference

Every rule's `name` follows the standard TextMate scope-naming convention (`keyword.control.*`,
`string.quoted.double.*`, `comment.line.double-slash.*`, ...) so it inherits sensible default
colors from *any* standard TextMate-compatible color theme without needing a Peregrine-specific
theme to be written at all.
