# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The gate: policy, narrowed token, verb, revoke. There is no other way to run a verb."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol

from lktec_bot.application.modules import (
    MODULES,
    ModuleSpec,
    authorize,
    authorize_module,
    validate_policy,
)
from lktec_bot.domain.permissions import Target, Verb, as_token_request, permissions_for
from lktec_bot.domain.policy import POLICY_PATH, Policy, PolicyError
from lktec_bot.ports.github_api import GitHubApi, GitHubApiFactory, RateLimit
from lktec_bot.ports.token_provider import Token, TokenError, TokenProvider
from lktec_bot.text import escape_markdown, sanitize

# Reading the policy file needs nothing but contents:read.
POLICY_READ_PERMISSIONS: Mapping[str, str] = {"contents": "read"}
# GitHub adds metadata:read to every installation token whether or not it was requested.
IMPLICIT_PERMISSIONS: Mapping[str, str] = {"metadata": "read"}
TOKEN_NOT_REVOKED = "token not revoked"  # noqa: S105 - a warning prefix, not a credential

# fullmatch, not match with ``$``: ``$`` also accepts a trailing newline.
_NAME = re.compile(r"[A-Za-z0-9_.-]+")
_ACCESS_RANK = {"read": 1, "write": 2}

# Called with every minted token value before anything else happens with it (``::add-mask::``).
Masker = Callable[[str], None]


def validate_name(kind: str, value: str) -> str:
    """An owner or repository name: non-empty, ``[A-Za-z0-9_.-]`` only, not ``.`` or ``..``."""
    if not value:
        raise ValueError(f"{kind} must not be empty")
    if value in (".", ".."):
        raise ValueError(f"{kind} must not be {value!r}")
    if not _NAME.fullmatch(value):
        raise ValueError(f"{kind} {value!r} may only contain letters, digits, '-', '_' and '.'")
    return value


def parse_repository(text: str) -> tuple[str, str]:
    """Split ``owner/name`` into its validated parts; exactly one slash is required."""
    if text.count("/") != 1:
        raise ValueError("repository must be owner/name with exactly one slash")
    owner, _, repo = text.partition("/")
    return validate_name("owner", owner), validate_name("repository name", repo)


class VerbOutcome(StrEnum):
    OK = "ok"
    DENIED = "denied"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class VerbRequest:
    """What a module asks the core to do. Owner and repository names are validated."""

    owner: str
    repo: str
    module: str
    verb: Verb
    target: Target | None = None
    args: Mapping[str, object] = field(default_factory=dict)
    # Filled in by the gate from the minted token: the login the verb acts as, when known.
    actor: str | None = None

    def __post_init__(self) -> None:
        validate_name("owner", self.owner)
        validate_name("repository name", self.repo)

    @property
    def repository(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True, slots=True)
class VerbResult:
    """Every run ends in one of these; ``denied`` and ``error`` are results, not exceptions.

    ``warnings`` carries what went wrong without changing the outcome, e.g. a token that could
    not be revoked. ``outputs`` are extra ``$GITHUB_OUTPUT`` keys a verb reports next to
    ``result``, ``message`` and ``url`` (``decision``, ``tag``, ``number``).
    """

    outcome: VerbOutcome
    message: str
    url: str | None = None
    warnings: tuple[str, ...] = ()
    outputs: Mapping[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome is VerbOutcome.OK

    def with_warnings(self, *warnings: str) -> VerbResult:
        return replace(self, warnings=(*self.warnings, *warnings))

    def to_markdown(self, request: VerbRequest) -> str:
        """One summary line, with every free-text part escaped, plus the URL and warnings."""
        label = {VerbOutcome.OK: "OK", VerbOutcome.DENIED: "DENIED", VerbOutcome.ERROR: "ERROR"}
        head = f"**{label[self.outcome]}** `{request.verb.value}` on `{request.repository}`"
        line = f"{head} ({escape_markdown(request.module)}): {escape_markdown(self.message)}"
        if self.url:
            line += f"\n\n<{sanitize(self.url).replace('<', '').replace('>', '').strip()}>"
        for warning in self.warnings:
            line += f"\n\nWarning: {escape_markdown(warning)}"
        return line + "\n"


@dataclass(frozen=True, slots=True)
class ReadResult[T]:
    """What :meth:`Gate.read` hands back: the reader's value, or why it did not run.

    ``failure`` is a ``denied`` or ``error`` :class:`VerbResult` (policy denial, token problem,
    a reader that raised); exactly one of the two fields is set. ``warnings`` carries what went
    wrong without changing the outcome (a token that could not be revoked).
    """

    value: T | None = None
    failure: VerbResult | None = None
    warnings: tuple[str, ...] = ()


class VerbHandler(Protocol):
    """A verb: given a client bound to the narrowed token, do one thing and report."""

    def __call__(self, api: GitHubApi, request: VerbRequest) -> VerbResult: ...


class Gate:
    """Runs a verb through policy and least privilege.

    1. The module must be known to the registry, before anything is minted.
    2. Load ``.github/lktec-bot.yml`` from the target repository's default branch, with a
       token narrowed to ``contents:read`` or with the caller's token when one is passed.
    3. Decide with :func:`~lktec_bot.application.modules.authorize`; a denial is returned, not
       raised.
    4. Mint a token narrowed to exactly the verb's permissions, check that what GitHub granted
       is no more than that (fail closed otherwise), run the verb, revoke the token.

    Every token is revoked whether or not its user succeeded. A revoke that fails is retried
    once and then reported as a warning on the result; it never replaces the result.
    """

    def __init__(
        self,
        tokens: TokenProvider,
        api_factory: GitHubApiFactory,
        handlers: Mapping[Verb, VerbHandler],
        *,
        modules: Mapping[str, ModuleSpec] | None = None,
        caller_token: str | None = None,
        policy_path: str = POLICY_PATH,
        mask: Masker | None = None,
    ) -> None:
        self._tokens = tokens
        self._api_factory = api_factory
        self._handlers = handlers
        self._modules = MODULES if modules is None else modules
        self._caller_token = caller_token
        self._policy_path = policy_path
        self._mask = mask
        # The budget the last client reported; the CLI prints it, nothing decides on it.
        self.last_rate_limit: RateLimit | None = None

    def close(self) -> None:
        """Release the token provider's connections."""
        self._tokens.close()

    def run(self, request: VerbRequest) -> VerbResult:
        handler = self._handlers.get(request.verb)
        if handler is None:
            return VerbResult(
                VerbOutcome.ERROR, f"no handler registered for verb {request.verb.value!r}"
            )
        if request.module not in self._modules:
            reason = authorize(None, request.module, request.verb, self._modules).reason
            return VerbResult(VerbOutcome.DENIED, reason)

        warnings: list[str] = []
        policy, failure = self._policy_or_failure(request.owner, request.repo, warnings)
        if failure is not None:
            return failure

        decision = authorize(policy, request.module, request.verb, self._modules)
        if not decision.allowed:
            return VerbResult(VerbOutcome.DENIED, decision.reason).with_warnings(*warnings)

        try:
            requested = as_token_request(permissions_for(request.verb, request.target))
        except ValueError as exc:
            return VerbResult(VerbOutcome.ERROR, str(exc)).with_warnings(*warnings)

        try:
            token = self._mint(request.owner, request.repo, requested)
        except TokenError as exc:
            result = VerbResult(VerbOutcome.ERROR, f"could not mint a token: {exc}")
            return result.with_warnings(*warnings)
        except Exception as exc:  # a bug in the provider, not a GitHub answer
            message = f"unexpected error while minting a token ({type(exc).__name__}): {exc}"
            return VerbResult(VerbOutcome.ERROR, message).with_warnings(*warnings)

        problem = self._scope_problem(token, requested, request.repo)
        if problem is not None:
            self._revoke_safely(token, warnings)
            result = VerbResult(VerbOutcome.ERROR, f"verb token refused: {problem}")
            return result.with_warnings(*warnings)

        try:
            with self._client(token.value) as api:
                result = handler(api, replace(request, actor=token.actor))
        except Exception as exc:
            result = VerbResult(VerbOutcome.ERROR, f"{request.verb.value} failed: {exc}")
        finally:
            self._revoke_safely(token, warnings)
        return result.with_warnings(*warnings)

    def read[T](
        self,
        owner: str,
        repo: str,
        module: str,
        permissions: Mapping[str, str],
        reader: Callable[[GitHubApi, Policy], T],
    ) -> ReadResult[T]:
        """Run a read-only ``reader`` for a module command, under the same rules as a verb.

        The module must be known and enabled in the policy; the reader gets a client bound to
        a token narrowed to exactly ``permissions`` (checked like a verb token and revoked
        afterwards) and the validated policy, so it can read its own options. Nothing a reader
        does may write: writes are verbs and go through :meth:`run`.
        """
        validate_name("owner", owner)
        validate_name("repository name", repo)
        if module not in self._modules:
            reason = authorize(None, module, Verb.PING, self._modules).reason
            return ReadResult(failure=VerbResult(VerbOutcome.DENIED, reason))
        warnings: list[str] = []
        policy, failure = self._policy_or_failure(owner, repo, warnings)
        if failure is not None:
            return ReadResult(failure=failure)
        decision = authorize_module(policy, module, self._modules)
        if policy is None or not decision.allowed:
            denied = VerbResult(VerbOutcome.DENIED, decision.reason)
            return ReadResult(failure=denied.with_warnings(*warnings))

        try:
            token = self._mint(owner, repo, permissions)
        except TokenError as exc:
            result = VerbResult(VerbOutcome.ERROR, f"could not mint a token: {exc}")
            return ReadResult(failure=result.with_warnings(*warnings))
        except Exception as exc:  # a bug in the provider, not a GitHub answer
            message = f"unexpected error while minting a token ({type(exc).__name__}): {exc}"
            return ReadResult(
                failure=VerbResult(VerbOutcome.ERROR, message).with_warnings(*warnings)
            )

        problem = self._scope_problem(token, permissions, repo)
        if problem is not None:
            self._revoke_safely(token, warnings)
            result = VerbResult(VerbOutcome.ERROR, f"read token refused: {problem}")
            return ReadResult(failure=result.with_warnings(*warnings))
        try:
            with self._client(token.value) as api:
                outcome = ReadResult(value=reader(api, policy))
        except Exception as exc:
            failed = VerbResult(VerbOutcome.ERROR, f"{module} could not read: {exc}")
            outcome = ReadResult(failure=failed)
        finally:
            self._revoke_safely(token, warnings)
        return replace(outcome, warnings=tuple(warnings))

    # -- helpers ---------------------------------------------------------------------------

    @contextmanager
    def _client(self, token: str) -> Iterator[GitHubApi]:
        with closing(self._api_factory(token)) as api:
            try:
                yield api
            finally:
                if api.last_rate_limit is not None:
                    self.last_rate_limit = api.last_rate_limit

    def _mint(self, owner: str, repo: str, permissions: Mapping[str, str]) -> Token:
        token = self._tokens.token_for(owner, repo, permissions)
        if self._mask is not None:
            self._mask(token.value)
        return token

    def _policy_or_failure(
        self, owner: str, repo: str, warnings: list[str]
    ) -> tuple[Policy | None, VerbResult | None]:
        """The policy (``None`` when the file is missing), or the result that ends the run."""
        try:
            return self._load_policy(owner, repo, warnings), None
        except PolicyError as exc:
            reasons = "; ".join(exc.reasons)
            result = VerbResult(VerbOutcome.DENIED, f"{self._policy_path} is invalid: {reasons}")
            return None, result.with_warnings(*warnings)
        except TokenError as exc:
            result = VerbResult(VerbOutcome.ERROR, f"could not mint the policy token: {exc}")
            return None, result.with_warnings(*warnings)
        except Exception as exc:  # every failure must become a result, never a traceback
            message = f"could not read {self._policy_path}: {type(exc).__name__}: {exc}"
            return None, VerbResult(VerbOutcome.ERROR, message).with_warnings(*warnings)

    def _load_policy(self, owner: str, repo: str, warnings: list[str]) -> Policy | None:
        if self._caller_token is not None:
            with self._client(self._caller_token) as api:
                return self._read_policy(api, owner, repo)
        token = self._mint(owner, repo, POLICY_READ_PERMISSIONS)
        try:
            problem = self._scope_problem(token, POLICY_READ_PERMISSIONS, repo)
            if problem is not None:
                raise TokenError(f"policy token refused: {problem}")
            with self._client(token.value) as api:
                return self._read_policy(api, owner, repo)
        finally:
            self._revoke_safely(token, warnings)

    def _read_policy(self, api: GitHubApi, owner: str, repo: str) -> Policy | None:
        # No ref: the policy comes from the default branch, never from a pull request head,
        # so a PR cannot bring its own permissions along.
        text = api.get_file_content(owner, repo, self._policy_path)
        if text is None:
            return None
        return validate_policy(text, self._modules)

    def _revoke_safely(self, token: Token, warnings: list[str]) -> None:
        """Revoke with one retry; a failure becomes a warning, never an exception."""
        failure = ""
        for _ in range(2):
            try:
                token.revoke()
                return
            except Exception as exc:
                failure = f"{type(exc).__name__}: {exc}"
        warnings.append(f"{TOKEN_NOT_REVOKED} ({failure})")

    @staticmethod
    def _scope_problem(token: Token, requested: Mapping[str, str], repo: str) -> str | None:
        """Why the token is broader than asked for, or ``None`` when it is not.

        Unknown scope counts as too broad: a provider that does not report permissions or
        repositories never gets to run a verb.
        """
        granted = token.permissions
        if granted is None:
            return "the provider did not report the token's permissions"
        allowed = {**IMPLICIT_PERMISSIONS, **requested}
        extra = sorted(
            f"{name}:{access}"
            for name, access in granted.items()
            if name not in allowed
            or _ACCESS_RANK.get(access, 99) > _ACCESS_RANK.get(allowed[name], 0)
        )
        if extra:
            wanted = ", ".join(f"{name}:{access}" for name, access in sorted(allowed.items()))
            return f"it carries {', '.join(extra)} beyond the requested {wanted}"
        repositories = token.repositories
        if repositories is None:
            return "the provider did not report the token's repositories"
        if {name.lower() for name in repositories} != {repo.lower()}:
            return f"it is scoped to {sorted(repositories)} instead of {repo!r}"
        return None
