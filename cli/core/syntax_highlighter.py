"""Dependency-free terminal syntax highlighter for fenced code blocks.

This module provides :class:`SyntaxHighlighter`, a small, self-contained
tokenizer that paints source code with ANSI colors for the TUI. It deliberately
avoids any third-party dependency (e.g. Pygments) so the CLI stays lightweight
and offline-friendly; instead it uses a generic, regex-driven tokenizer driven
by a per-language :class:`LanguageDefinition` (keywords, comment markers, string
quoting rules, etc.).

The goal is broad "good enough" coverage of the languages models commonly emit,
not a full grammar for each one. Unknown languages fall back to a generic
C-like profile so they still get string/number/comment coloring.

Token coloring reuses the project's shared ANSI helpers in
``console_utils`` so it honors ``NO_COLOR`` / ``FORCE_COLOR`` / Windows VT and
matches the rest of the CLI palette. When color is disabled the input is
returned verbatim.

Use :func:`highlight_code` for a one-shot call, or instantiate
:class:`SyntaxHighlighter` once and reuse it across many blocks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .console_utils import _ansi_rgb, _stdout_color_enabled

# --- Palette (One Dark-ish, matching the CLI's existing syntax-green) --------
# Each helper paints a token; they all collapse to identity when color is off.


def _c_keyword(text: str) -> str:
    return _ansi_rgb(text, 198, 120, 221)  # purple


def _c_string(text: str) -> str:
    return _ansi_rgb(text, 152, 195, 121)  # green


def _c_comment(text: str) -> str:
    return _ansi_rgb(text, 92, 99, 112)  # gray


def _c_number(text: str) -> str:
    return _ansi_rgb(text, 209, 154, 102)  # orange


def _c_function(text: str) -> str:
    return _ansi_rgb(text, 97, 175, 239)  # blue


def _c_builtin(text: str) -> str:
    return _ansi_rgb(text, 86, 182, 194)  # cyan


def _c_decorator(text: str) -> str:
    return _ansi_rgb(text, 229, 192, 123)  # yellow


def _c_plain(text: str) -> str:
    return text


@dataclass(frozen=True)
class LanguageDefinition:
    """Per-language lexing configuration used by :class:`SyntaxHighlighter`.

    The tokenizer is generic; a definition only customizes the language-specific
    vocabulary and lexical rules. All fields are optional with sensible C-like
    defaults so a new language can often be added with just ``keywords``.
    """

    name: str
    keywords: frozenset = field(default_factory=frozenset)
    builtins: frozenset = field(default_factory=frozenset)
    # Single-line comment prefixes (e.g. ``//``, ``#``, ``--``).
    line_comments: Tuple[str, ...] = ("//",)
    # Block comment (open, close) pairs (e.g. ``/* */``).
    block_comments: Tuple[Tuple[str, str], ...] = (("/*", "*/"),)
    # String quote characters. Each is treated as both open and close.
    string_quotes: Tuple[str, ...] = ('"', "'")
    # When True, a backslash escapes the next char inside a string.
    backslash_escapes: bool = True
    # Identifier char class (without the surrounding brackets).
    ident_chars: str = r"A-Za-z_$\u00c0-\uffff0-9"
    # Prefix that marks a decorator/annotation token (e.g. ``@`` in Python/Java).
    decorator_prefix: Optional[str] = None


# --- Shared keyword groups ---------------------------------------------------

_PY_KW = frozenset(
    "False None True and as assert async await break class continue def del elif "
    "else except finally for from global if import in is lambda nonlocal not or "
    "pass raise return try while with yield match case".split()
)
_PY_BUILTINS = frozenset(
    "print len range int str float list dict set tuple bool bytes object type "
    "isinstance issubclass super self cls open enumerate zip map filter sorted "
    "sum min max abs all any repr format input getattr setattr hasattr".split()
)

_C_KW = frozenset(
    "auto break case char const continue default do double else enum extern float "
    "for goto if inline int long register restrict return short signed sizeof "
    "static struct switch typedef union unsigned void volatile while bool true "
    "false".split()
)
_CPP_KW = _C_KW | frozenset(
    "class namespace template typename public private protected virtual override "
    "final new delete this nullptr using friend operator explicit constexpr "
    "noexcept mutable try catch throw decltype auto static_cast dynamic_cast "
    "reinterpret_cast const_cast and or not".split()
)
_JAVA_KW = frozenset(
    "abstract assert boolean break byte case catch char class const continue "
    "default do double else enum extends final finally float for goto if "
    "implements import instanceof int interface long native new package private "
    "protected public return short static strictfp super switch synchronized this "
    "throw throws transient try void volatile while var record sealed yield "
    "true false null".split()
)
_JS_KW = frozenset(
    "abstract arguments await break case catch class const continue debugger "
    "default delete do else enum export extends false finally for from function "
    "get if implements import in instanceof interface let new null of package "
    "private protected public return set static super switch this throw true try "
    "typeof var void while with yield async await as".split()
)
_TS_KW = _JS_KW | frozenset(
    "any boolean number string symbol type namespace declare readonly keyof infer "
    "is unknown never object module abstract implements".split()
)
_CS_KW = frozenset(
    "abstract as base bool break byte case catch char checked class const continue "
    "decimal default delegate do double else enum event explicit extern false "
    "finally fixed float for foreach goto if implicit in int interface internal is "
    "lock long namespace new null object operator out override params private "
    "protected public readonly ref return sbyte sealed short sizeof stackalloc "
    "static string struct switch this throw true try typeof uint ulong unchecked "
    "unsafe ushort using virtual void volatile while var async await yield "
    "nameof".split()
)
_GO_KW = frozenset(
    "break case chan const continue default defer else fallthrough for func go "
    "goto if import interface map package range return select struct switch type "
    "var nil true false iota make new len cap append".split()
)
_RUST_KW = frozenset(
    "as async await break const continue crate dyn else enum extern false fn for "
    "if impl in let loop match mod move mut pub ref return self Self static struct "
    "super trait true type unsafe use where while async box".split()
)
_RUBY_KW = frozenset(
    "alias and begin break case class def defined? do else elsif end ensure false "
    "for if in module next nil not or redo rescue retry return self super then "
    "true undef unless until when while yield require attr_accessor puts".split()
)
_PHP_KW = frozenset(
    "abstract and array as break callable case catch class clone const continue "
    "declare default do echo else elseif empty enddeclare endfor endforeach endif "
    "endswitch endwhile extends final finally fn for foreach function global goto "
    "if implements include instanceof insteadof interface isset list namespace new "
    "or print private protected public require return static switch throw trait "
    "try unset use var while xor yield true false null".split()
)
_SWIFT_KW = frozenset(
    "associatedtype class deinit enum extension fileprivate func import init "
    "inout internal let open operator private protocol public rethrows static "
    "struct subscript typealias var break case continue default defer do else "
    "fallthrough for guard if in repeat return switch where while as catch throw "
    "throws try self Self super nil true false".split()
)
_KOTLIN_KW = frozenset(
    "as break class continue do else false for fun if in interface is null object "
    "package return super this throw true try typealias typeof val var when while "
    "by catch constructor delegate dynamic field finally get import init param "
    "property receiver set value where abstract final open override private public "
    "protected internal suspend companion data sealed inline".split()
)
_SQL_KW = frozenset(
    "select from where insert into values update set delete create table drop "
    "alter add column primary key foreign references index view join inner left "
    "right outer full on group by order having limit offset distinct union all as "
    "and or not null is in like between exists case when then else end count sum "
    "avg min max distinct asc desc default constraint unique".split()
)
_SH_KW = frozenset(
    "if then else elif fi for while until do done case esac function in select "
    "return break continue local export readonly declare echo cd source set unset "
    "trap exit".split()
)
_PS_KW = frozenset(
    "if else elseif switch foreach for while do until break continue return "
    "function filter param begin process end try catch finally throw trap class "
    "enum using namespace in".split()
)

# --- Language registry -------------------------------------------------------

_DEFINITIONS: Dict[str, LanguageDefinition] = {}


def _register(definition: LanguageDefinition, *aliases: str) -> None:
    _DEFINITIONS[definition.name.lower()] = definition
    for alias in aliases:
        _DEFINITIONS[alias.lower()] = definition


_register(
    LanguageDefinition(
        "python",
        keywords=_PY_KW,
        builtins=_PY_BUILTINS,
        line_comments=("#",),
        block_comments=(),
        string_quotes=('"""', "'''", '"', "'"),
        decorator_prefix="@",
    ),
    "py",
    "python3",
)
_register(
    LanguageDefinition(
        "c",
        keywords=_C_KW,
        line_comments=("//",),
        block_comments=(("/*", "*/"),),
    ),
    "h",
)
_register(
    LanguageDefinition(
        "cpp",
        keywords=_CPP_KW,
        line_comments=("//",),
        block_comments=(("/*", "*/"),),
    ),
    "c++",
    "cc",
    "cxx",
    "hpp",
    "hxx",
)
_register(
    LanguageDefinition("java", keywords=_JAVA_KW, decorator_prefix="@"),
)
_register(
    LanguageDefinition(
        "javascript",
        keywords=_JS_KW,
        line_comments=("//",),
        block_comments=(("/*", "*/"),),
        string_quotes=('"', "'", "`"),
    ),
    "js",
    "jsx",
    "node",
    "mjs",
    "cjs",
)
_register(
    LanguageDefinition(
        "typescript",
        keywords=_TS_KW,
        line_comments=("//",),
        block_comments=(("/*", "*/"),),
        string_quotes=('"', "'", "`"),
        decorator_prefix="@",
    ),
    "ts",
    "tsx",
)
_register(
    LanguageDefinition("csharp", keywords=_CS_KW), "cs", "c#", "dotnet"
)
_register(
    LanguageDefinition(
        "go",
        keywords=_GO_KW,
        line_comments=("//",),
        block_comments=(("/*", "*/"),),
        string_quotes=('"', "`"),
    ),
    "golang",
)
_register(
    LanguageDefinition(
        "rust",
        keywords=_RUST_KW,
        line_comments=("//",),
        block_comments=(("/*", "*/"),),
    ),
    "rs",
)
_register(
    LanguageDefinition(
        "ruby",
        keywords=_RUBY_KW,
        line_comments=("#",),
        block_comments=(),
    ),
    "rb",
)
_register(
    LanguageDefinition(
        "php",
        keywords=_PHP_KW,
        line_comments=("//", "#"),
        block_comments=(("/*", "*/"),),
    ),
)
_register(
    LanguageDefinition("swift", keywords=_SWIFT_KW, decorator_prefix="@"),
)
_register(
    LanguageDefinition("kotlin", keywords=_KOTLIN_KW, decorator_prefix="@"),
    "kt",
    "kts",
)
_register(
    LanguageDefinition(
        "sql",
        keywords=_SQL_KW,
        line_comments=("--",),
        block_comments=(("/*", "*/"),),
        string_quotes=("'", '"'),
    ),
    "mysql",
    "postgres",
    "postgresql",
    "psql",
    "sqlite",
)
_register(
    LanguageDefinition(
        "bash",
        keywords=_SH_KW,
        line_comments=("#",),
        block_comments=(),
        string_quotes=('"', "'"),
    ),
    "sh",
    "shell",
    "zsh",
    "console",
)
_register(
    LanguageDefinition(
        "powershell",
        keywords=_PS_KW,
        line_comments=("#",),
        block_comments=(("<#", "#>"),),
        string_quotes=('"', "'"),
    ),
    "ps1",
    "ps",
    "pwsh",
)
_register(
    LanguageDefinition(
        "json",
        keywords=frozenset("true false null".split()),
        line_comments=(),
        block_comments=(),
        string_quotes=('"',),
    ),
)
_register(
    LanguageDefinition(
        "yaml",
        keywords=frozenset("true false null yes no on off".split()),
        line_comments=("#",),
        block_comments=(),
        string_quotes=('"', "'"),
    ),
    "yml",
)
_register(
    LanguageDefinition(
        "toml",
        keywords=frozenset("true false".split()),
        line_comments=("#",),
        block_comments=(),
        string_quotes=('"', "'"),
    ),
)
_register(
    LanguageDefinition(
        "ini",
        keywords=frozenset(),
        line_comments=(";", "#"),
        block_comments=(),
        string_quotes=('"', "'"),
    ),
    "cfg",
    "conf",
    "properties",
)
_register(
    LanguageDefinition(
        "dockerfile",
        keywords=frozenset(
            "FROM RUN CMD LABEL MAINTAINER EXPOSE ENV ADD COPY ENTRYPOINT VOLUME "
            "USER WORKDIR ARG ONBUILD STOPSIGNAL HEALTHCHECK SHELL".split()
        ),
        line_comments=("#",),
        block_comments=(),
    ),
    "docker",
)

# Generic C-like fallback for unknown languages — still colors strings,
# numbers and comments.
_GENERIC = LanguageDefinition(
    "generic",
    keywords=frozenset(),
    line_comments=("//", "#"),
    block_comments=(("/*", "*/"),),
    string_quotes=('"', "'", "`"),
)


def supported_languages() -> List[str]:
    """Return the sorted list of canonical language names (no aliases)."""
    seen = {}
    for key, definition in _DEFINITIONS.items():
        seen[definition.name] = True
    return sorted(seen.keys())


def supported_aliases() -> Dict[str, List[str]]:
    """Map each canonical language to the aliases that resolve to it."""
    out: Dict[str, List[str]] = {}
    for key, definition in _DEFINITIONS.items():
        if key == definition.name:
            continue
        out.setdefault(definition.name, []).append(key)
    for name in out:
        out[name].sort()
    return dict(sorted(out.items()))


class SyntaxHighlighter:
    """Generic, regex-driven ANSI syntax highlighter.

    Construct once and call :meth:`highlight` per code block. The highlighter is
    stateless across calls, so a single instance can be shared freely.
    """

    def __init__(self) -> None:
        # Cache compiled identifier/number regexes per language config to avoid
        # recompiling for every line of a large block.
        self._ident_re_cache: Dict[str, "re.Pattern[str]"] = {}

    @staticmethod
    def resolve_language(lang: Optional[str]) -> Optional[LanguageDefinition]:
        """Return the definition for ``lang`` (case/alias-insensitive) or None."""
        if not lang:
            return None
        return _DEFINITIONS.get(str(lang).strip().lower())

    def highlight(self, code: str, lang: Optional[str] = None) -> str:
        """Return ``code`` with ANSI color, lexed as ``lang``.

        Unknown or missing languages use a generic C-like profile. When color
        output is disabled (``NO_COLOR`` etc.), the input is returned verbatim.
        """
        if not isinstance(code, str) or not code:
            return code or ""
        if not _stdout_color_enabled():
            return code
        definition = self.resolve_language(lang) or _GENERIC
        # Highlight line-by-line so the existing width-aware wrapping/border
        # pipeline keeps working; block comments are tracked across lines.
        out_lines: List[str] = []
        in_block_comment = False
        block_close = ""
        for line in code.split("\n"):
            rendered, in_block_comment, block_close = self._highlight_line(
                line, definition, in_block_comment, block_close
            )
            out_lines.append(rendered)
        return "\n".join(out_lines)

    def highlight_line_stateful(
        self,
        line: str,
        lang: Optional[str],
        in_block_comment: bool = False,
        block_close: str = "",
    ) -> Tuple[str, bool, str]:
        """Highlight a single line, carrying multi-line block-comment state.

        Returns ``(rendered_line, in_block_comment, block_close)``. Callers that
        render a fenced block line-by-line (e.g. the TUI streaming renderer)
        thread the returned state into the next call so block comments spanning
        several lines stay colored. Returns the line verbatim when color is
        disabled.
        """
        if not _stdout_color_enabled():
            return line, in_block_comment, block_close
        definition = self.resolve_language(lang) or _GENERIC
        return self._highlight_line(line, definition, in_block_comment, block_close)

    # -- internals -----------------------------------------------------------

    def _ident_re(self, definition: LanguageDefinition) -> "re.Pattern[str]":
        cached = self._ident_re_cache.get(definition.name)
        if cached is None:
            cached = re.compile(rf"[{definition.ident_chars}]+")
            self._ident_re_cache[definition.name] = cached
        return cached

    def _highlight_line(
        self,
        line: str,
        definition: LanguageDefinition,
        in_block_comment: bool,
        block_close: str,
    ) -> Tuple[str, bool, str]:
        if line == "":
            return "", in_block_comment, block_close
        out: List[str] = []
        i = 0
        n = len(line)
        ident_re = self._ident_re(definition)

        # Continuation of a multi-line block comment from a previous line.
        if in_block_comment:
            close_at = line.find(block_close) if block_close else -1
            if close_at < 0:
                return _c_comment(line), True, block_close
            end = close_at + len(block_close)
            out.append(_c_comment(line[:end]))
            i = end
            in_block_comment = False
            block_close = ""

        while i < n:
            ch = line[i]

            # Line comment.
            matched_line_comment = False
            for marker in definition.line_comments:
                if marker and line.startswith(marker, i):
                    out.append(_c_comment(line[i:]))
                    i = n
                    matched_line_comment = True
                    break
            if matched_line_comment:
                break

            # Block comment open.
            matched_block = False
            for open_marker, close_marker in definition.block_comments:
                if open_marker and line.startswith(open_marker, i):
                    close_at = line.find(close_marker, i + len(open_marker))
                    if close_at < 0:
                        out.append(_c_comment(line[i:]))
                        return "".join(out), True, close_marker
                    end = close_at + len(close_marker)
                    out.append(_c_comment(line[i:end]))
                    i = end
                    matched_block = True
                    break
            if matched_block:
                continue

            # String literal.
            string_token = self._consume_string(line, i, definition)
            if string_token is not None:
                token_text, new_i = string_token
                out.append(_c_string(token_text))
                i = new_i
                continue

            # Decorator / annotation (e.g. ``@property``).
            if (
                definition.decorator_prefix
                and ch == definition.decorator_prefix
                and i + 1 < n
                and (line[i + 1].isalpha() or line[i + 1] == "_")
            ):
                m = ident_re.match(line, i + 1)
                if m:
                    out.append(_c_decorator(line[i : m.end()]))
                    i = m.end()
                    continue

            # Number literal.
            if ch.isdigit() or (
                ch == "." and i + 1 < n and line[i + 1].isdigit()
            ):
                num = self._consume_number(line, i)
                out.append(_c_number(line[i:num]))
                i = num
                continue

            # Identifier / keyword / builtin / function call.
            if ch.isalpha() or ch == "_" or ord(ch) > 0x7F:
                m = ident_re.match(line, i)
                if m:
                    word = m.group(0)
                    end = m.end()
                    if word in definition.keywords:
                        out.append(_c_keyword(word))
                    elif word in definition.builtins:
                        out.append(_c_builtin(word))
                    elif end < n and line[end] == "(":
                        out.append(_c_function(word))
                    else:
                        out.append(word)
                    i = end
                    continue

            # Anything else (punctuation, whitespace) stays plain.
            out.append(ch)
            i += 1

        return "".join(out), in_block_comment, block_close

    @staticmethod
    def _consume_string(
        line: str, start: int, definition: LanguageDefinition
    ) -> Optional[Tuple[str, int]]:
        """Consume a single-line string starting at ``start``.

        Returns ``(token_text, end_index)`` or ``None`` when no string begins
        here. Triple-quoted markers are matched first (longest-first ordering)
        so a triple quote wins over a single quote. Unterminated strings (e.g. a
        multi-line Python triple-quoted block) consume to end-of-line so the
        rest of the line still reads as a string.
        """
        n = len(line)
        # Order quotes longest-first so triple quotes beat single quotes.
        for quote in sorted(definition.string_quotes, key=len, reverse=True):
            if not quote or not line.startswith(quote, start):
                continue
            i = start + len(quote)
            while i < n:
                if definition.backslash_escapes and line[i] == "\\":
                    i += 2
                    continue
                if line.startswith(quote, i):
                    return line[start : i + len(quote)], i + len(quote)
                i += 1
            # Unterminated on this line: consume the remainder.
            return line[start:], n
        return None

    @staticmethod
    def _consume_number(line: str, start: int) -> int:
        """Return the end index of a numeric literal beginning at ``start``."""
        n = len(line)
        i = start
        # Hex / binary / octal prefixes.
        if (
            line[i] == "0"
            and i + 1 < n
            and line[i + 1] in "xXbBoO"
        ):
            i += 2
            while i < n and (line[i].isalnum() or line[i] == "_"):
                i += 1
            return i
        seen_dot = False
        seen_exp = False
        while i < n:
            c = line[i]
            if c.isdigit() or c == "_":
                i += 1
            elif c == "." and not seen_dot and not seen_exp:
                seen_dot = True
                i += 1
            elif c in "eE" and not seen_exp:
                seen_exp = True
                i += 1
                if i < n and line[i] in "+-":
                    i += 1
            else:
                break
        # Trailing type suffix (e.g. ``10L``, ``3.14f``, ``5u``).
        while i < n and line[i] in "fFlLuUdD":
            i += 1
        return i


# Module-level singleton for convenient one-shot use.
_DEFAULT_HIGHLIGHTER = SyntaxHighlighter()


def highlight_code(code: str, lang: Optional[str] = None) -> str:
    """Highlight ``code`` as ``lang`` using the shared default highlighter."""
    return _DEFAULT_HIGHLIGHTER.highlight(code, lang)
