"""Verb registry. Adding a verb is one new file in this package.

A verb module declares its handler, its grants and its command line options together::

    @register(
        Verb.APPROVE,
        grants=frozenset({Grant(Permission.PULL_REQUESTS, "write")}),
        options=(Option("number", "Pull request number.", kind="int", required=True),),
    )
    def approve(api: GitHubApi, request: VerbRequest) -> VerbResult: ...

The canonical permission table stays in ``lktec_bot/domain/permissions.py`` so the footprint
of every planned verb is reviewed in one place; ``register`` refuses a handler whose declared
grants differ from that table, so the two cannot drift apart. :func:`load_verbs` imports every
module of this package with ``pkgutil`` and returns the registry; nothing is imported at
package import time, which is what keeps the import graph acyclic.

The options are declarative (name, kind, help); the CLI turns them into flags and the parsed
values arrive in ``VerbRequest.args`` under the option's name. The registry itself knows
nothing about typer.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from lktec_bot.application.gate import VerbHandler
from lktec_bot.domain.permissions import Grant, Target, Verb, permissions_for

# Either one grant set, or one per target for the verbs whose permission depends on it.
GrantSpec = frozenset[Grant] | Mapping[Target, frozenset[Grant]]

# ``text`` is a body: the CLI offers both ``--<name>`` (inline) and ``--<name>-file`` (a path).
# ``multi`` is a string flag that may be repeated; its value is a list of strings.
# ``flag`` is a boolean switch: ``--name`` (off by default), or ``--name/--no-name`` when
# ``default`` is true. Its value is always a bool.
OptionKind = Literal["int", "str", "choice", "text", "multi", "flag"]

_OPTION_NAME = re.compile(r"^[a-z][a-z0-9_]*\Z")


@dataclass(frozen=True, slots=True)
class Option:
    """One command line option of a verb; its value lands in ``VerbRequest.args[name]``."""

    name: str
    help: str
    kind: OptionKind = "str"
    required: bool = False
    choices: tuple[str, ...] = ()
    # Only for ``flag``: whether the switch is on when the command line does not mention it.
    default: bool = False
    # The command line spelling when it differs from the name (``from`` is a Python keyword).
    cli_flag: str | None = None

    def __post_init__(self) -> None:
        if not _OPTION_NAME.match(self.name):
            raise ValueError(f"option name {self.name!r} must be snake_case")
        if (self.kind == "choice") != bool(self.choices):
            raise ValueError(f"option {self.name!r}: choices go with kind='choice' only")
        if self.default and self.kind != "flag":
            raise ValueError(f"option {self.name!r}: default goes with kind='flag' only")

    @property
    def flag(self) -> str:
        return self.cli_flag or "--" + self.name.replace("_", "-")


@dataclass(frozen=True, slots=True)
class VerbSpec:
    verb: Verb
    handler: VerbHandler
    grants: GrantSpec
    options: tuple[Option, ...] = ()

    @property
    def takes_target(self) -> bool:
        """Whether the verb's permission depends on a target (``--target`` on the CLI)."""
        return isinstance(self.grants, Mapping)


REGISTRY: dict[Verb, VerbSpec] = {}


def register(
    verb: Verb, *, grants: GrantSpec, options: tuple[Option, ...] = ()
) -> Callable[[VerbHandler], VerbHandler]:
    """Register ``handler`` for ``verb``; ``grants`` must equal the domain table's entry."""

    def decorator(handler: VerbHandler) -> VerbHandler:
        if verb in REGISTRY:
            raise ValueError(f"verb {verb.value!r} is already registered")
        _check_grants(verb, grants)
        names = [option.name for option in options]
        if len(set(names)) != len(names):
            raise ValueError(f"verb {verb.value!r} declares an option twice")
        REGISTRY[verb] = VerbSpec(verb, handler, grants, options)
        return handler

    return decorator


def load_verbs() -> Mapping[Verb, VerbSpec]:
    """Import every verb module once so each ``@register`` runs, then return the registry."""
    for info in pkgutil.iter_modules(__path__):
        if not info.name.startswith("_"):
            importlib.import_module(f"{__name__}.{info.name}")
    return REGISTRY


def handlers() -> dict[Verb, VerbHandler]:
    """The handler map the gate takes."""
    return {verb: spec.handler for verb, spec in load_verbs().items()}


def _check_grants(verb: Verb, grants: GrantSpec) -> None:
    targets: list[Target | None] = list(Target) if verb is Verb.COMMENT else [None]
    expected: dict[Target | None, frozenset[Grant]] = {
        target: permissions_for(verb, target) for target in targets
    }
    declared: dict[Target | None, frozenset[Grant]] = (
        {target: grant_set for target, grant_set in grants.items()}
        if isinstance(grants, Mapping)
        else {None: grants}
    )
    if declared != expected:
        raise ValueError(
            f"verb {verb.value!r} declares grants {_render(declared)} but the table in "
            f"domain/permissions.py says {_render(expected)}"
        )


def _render(grants: Mapping[Target | None, frozenset[Grant]]) -> str:
    parts = []
    for target, grant_set in sorted(grants.items(), key=lambda item: str(item[0])):
        names = ", ".join(sorted(str(grant) for grant in grant_set))
        parts.append(f"{target.value}: {names}" if target is not None else names)
    return "; ".join(parts)


__all__ = [
    "REGISTRY",
    "GrantSpec",
    "Option",
    "OptionKind",
    "VerbSpec",
    "handlers",
    "load_verbs",
    "register",
]
