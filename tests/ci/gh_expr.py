"""A small evaluator for the GitHub Actions expressions that `tests/ci` pins.

The workflow contract tests used to compare expression *strings*, which pins the
spelling and not the behaviour. This module evaluates the expressions the CI
workflows actually use (`||`, `&&`, `!`, `==`, `!=`, parentheses, string and number
literals, dotted context lookups, `format`, `contains`, `always`, `cancelled`) against
a context dict, so a test can ask "what does `runs-on` resolve to on a pull_request
when the runner variable is set?".

Anything outside that grammar raises `ExpressionError` instead of guessing: a test
must never pass because the evaluator silently ignored a construct.

Semantics follow the GitHub docs: `a || b` and `a && b` return operands, not
booleans; falsy values are `null`, `''`, `0` and `false`; string comparison is case
insensitive; a missing context property is `null`.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["ExpressionError", "evaluate", "truthy"]


class ExpressionError(ValueError):
    """The expression uses something this evaluator does not model."""


_TOKEN = re.compile(
    r"""
    \s*(?:
      (?P<string>'(?:[^']|'')*')
    | (?P<number>\d+(?:\.\d+)?)
    | (?P<op>\|\||&&|==|!=|!|\(|\)|,)
    | (?P<name>[A-Za-z_][A-Za-z0-9_\-]*(?:\.[A-Za-z0-9_\-]+)*)
    )""",
    re.VERBOSE,
)


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(text):
        if not text[position:].strip():
            break
        match = _TOKEN.match(text, position)
        if match is None or match.end() == position:
            raise ExpressionError(f"cannot tokenize {text[position:]!r} in {text!r}")
        kind = match.lastgroup
        assert kind is not None
        tokens.append((kind, match.group(kind)))
        position = match.end()
    return tokens


def truthy(value: Any) -> bool:
    return value not in (None, "", 0, False)


def _loose_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return (left in (None, "", 0)) and (right in (None, "", 0))
    if isinstance(left, str) and isinstance(right, str):
        return left.lower() == right.lower()
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return str(left).lower() == str(right).lower()


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]], context: dict[str, Any]):
        self.tokens = tokens
        self.context = context
        self.index = 0

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def _take(self) -> tuple[str, str]:
        token = self._peek()
        if token is None:
            raise ExpressionError("unexpected end of expression")
        self.index += 1
        return token

    def _accept(self, op: str) -> bool:
        token = self._peek()
        if token == ("op", op):
            self.index += 1
            return True
        return False

    def parse(self) -> Any:
        value = self._or()
        if self._peek() is not None:
            raise ExpressionError(f"trailing tokens: {self.tokens[self.index :]!r}")
        return value

    def _or(self) -> Any:
        left = self._and()
        while self._accept("||"):
            right = self._and()
            left = left if truthy(left) else right
        return left

    def _and(self) -> Any:
        left = self._equality()
        while self._accept("&&"):
            right = self._equality()
            left = right if truthy(left) else left
        return left

    def _equality(self) -> Any:
        left = self._unary()
        while True:
            if self._accept("=="):
                left = _loose_equal(left, self._unary())
            elif self._accept("!="):
                left = not _loose_equal(left, self._unary())
            else:
                return left

    def _unary(self) -> Any:
        if self._accept("!"):
            return not truthy(self._unary())
        return self._primary()

    def _primary(self) -> Any:
        kind, text = self._take()
        if kind == "string":
            return text[1:-1].replace("''", "'")
        if kind == "number":
            return float(text) if "." in text else int(text)
        if kind == "op" and text == "(":
            value = self._or()
            if not self._accept(")"):
                raise ExpressionError("missing ')'")
            return value
        if kind == "name":
            if self._accept("("):
                return self._call(text)
            return self._lookup(text)
        raise ExpressionError(f"unexpected token {text!r}")

    def _call(self, name: str) -> Any:
        args: list[Any] = []
        if not self._accept(")"):
            args.append(self._or())
            while self._accept(","):
                args.append(self._or())
            if not self._accept(")"):
                raise ExpressionError("missing ')' after arguments")
        if name == "always":
            return True
        if name == "cancelled":
            return bool(self.context.get("__cancelled__", False))
        if name == "format":
            template = _stringify(args[0])
            for position, argument in enumerate(args[1:]):
                template = template.replace(f"{{{position}}}", _stringify(argument))
            return template
        if name == "contains":
            haystack, needle = args
            if isinstance(haystack, (list, tuple)):
                return any(_loose_equal(item, needle) for item in haystack)
            return _stringify(needle).lower() in _stringify(haystack).lower()
        raise ExpressionError(f"unsupported function {name}()")

    def _lookup(self, dotted: str) -> Any:
        value: Any = self.context
        for part in dotted.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return None
        return value


_WRAPPED = re.compile(r"^\s*\$\{\{(.*?)\}\}\s*$", re.DOTALL)
_SEGMENT = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)


def evaluate(expression: str | bool, context: dict[str, Any]) -> Any:
    """Evaluate one workflow expression; `${{ }}` wrapping is optional.

    A whole-expression `${{ ... }}` returns the raw value. A string that mixes
    literal text with `${{ }}` segments is interpolated, as GitHub does, and the
    result is a string. A non-string (a YAML boolean) is returned as is.
    """

    if isinstance(expression, bool):
        return expression
    if "${{" not in expression:
        return _Parser(_tokenize(expression), context).parse()
    if len(_SEGMENT.findall(expression)) == 1:
        wrapped = _WRAPPED.match(expression)
        if wrapped:
            return _Parser(_tokenize(wrapped.group(1)), context).parse()

    def replace(match: re.Match[str]) -> str:
        return _stringify(_Parser(_tokenize(match.group(1)), context).parse())

    return _SEGMENT.sub(replace, expression)
