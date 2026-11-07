# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lktec_bot.application.gate import VerbOutcome, VerbRequest, VerbResult
from lktec_bot.cli import app
from lktec_bot.domain.permissions import Verb
from lktec_bot.ports.github_api import CheckRun
from tests.fakes import BOT_SLUG, FakeRepository
from tests.verb_helpers import HEAD, LOCKFILES, Harness

SHA = HEAD
URL = "https://github.com/acme/widgets/runs/"
MARKDOWN_SUMMARY = "Lock file regenerated."


def existing_run(run_id: int = 5, **changes: object) -> CheckRun:
    base = CheckRun(
        id=run_id,
        html_url=f"{URL}{run_id}",
        name="lock files",
        status="completed",
        conclusion="success",
        details_url=None,
        title="Lock files",
        summary=MARKDOWN_SUMMARY,
        text=None,
        app_id=1,
        app_slug=BOT_SLUG,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def setup(*runs: CheckRun) -> Harness:
    state = FakeRepository(check_runs={SHA: list(runs)} if runs else {})
    return Harness(state, policy=LOCKFILES)


def run_check(h: Harness, **args: object) -> VerbResult:
    base: dict[str, object] = {
        "sha": SHA,
        "name": "lock files",
        "title": "Lock files",
        "summary": MARKDOWN_SUMMARY,
        "conclusion": "success",
    }
    return h.run(Verb.CHECK, {**base, **args}, module="lockfiles")


def writes(h: Harness) -> list[tuple[str, int, dict[str, Any]]]:
    return h.state.check_calls


def write_annotations(tmp_path: Path, count: int) -> str:
    entries = [
        {
            "path": f"src/file{index}.py",
            "start_line": 1,
            "end_line": 2,
            "annotation_level": "warning",
            "message": f"problem {index}",
        }
        for index in range(count)
    ]
    path = tmp_path / "annotations.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return str(path)


def annotation(**changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "path": "src/a.py",
        "start_line": 3,
        "end_line": 4,
        "annotation_level": "notice",
        "message": "look here",
    }
    return {**base, **changes}


def annotations_file(tmp_path: Path, content: object) -> str:
    path = tmp_path / "annotations.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return str(path)


# -- create, update, unchanged ----------------------------------------------------------------


def test_creates_a_check_run_when_none_exists() -> None:
    h = setup()

    result = run_check(h)

    assert result.outcome is VerbOutcome.OK
    assert result.message == "created check run 'lock files' on 0123456 (success)"
    assert result.url == f"{URL}1"
    assert dict(result.outputs) == {"decision": "created", "id": "1"}
    assert writes(h) == [
        (
            "create",
            1,
            {
                "name": "lock files",
                "sha": SHA,
                "status": "completed",
                "conclusion": "success",
                "details_url": None,
                "external_id": None,
                "title": "Lock files",
                "summary": MARKDOWN_SUMMARY,
                "text": None,
                "annotations": [],
            },
        )
    ]


def test_creates_with_every_optional_field_and_strips_the_bodies() -> None:
    h = setup()

    result = run_check(
        h,
        summary="  summary \n\n",
        text="\n details \n",
        conclusion="action_required",
        details_url="https://example.com/run/1",
        external_id="run:1/a-b_c.d",
        name="  lock files ",
        title=" Lock files ",
    )

    assert result.ok
    payload = writes(h)[0][2]
    assert payload["name"] == "lock files"
    assert payload["title"] == "Lock files"
    assert payload["summary"] == "summary"
    assert payload["text"] == "details"
    assert payload["details_url"] == "https://example.com/run/1"
    assert payload["external_id"] == "run:1/a-b_c.d"
    assert payload["conclusion"] == "action_required"


def test_an_in_progress_run_has_no_conclusion_and_the_message_names_the_status() -> None:
    h = setup()

    result = run_check(h, status="in_progress", conclusion=None)

    assert result.message == "created check run 'lock files' on 0123456 (in_progress)"
    assert writes(h)[0][2]["status"] == "in_progress"
    assert writes(h)[0][2]["conclusion"] is None


def test_empty_optional_values_count_as_not_given() -> None:
    h = setup()

    result = run_check(h, text="  ", details_url="", external_id="", status="", annotations_file="")

    assert result.ok
    payload = writes(h)[0][2]
    assert payload["text"] is None
    assert payload["details_url"] is None
    assert payload["external_id"] is None
    assert payload["status"] == "completed"


def test_unchanged_when_everything_matches_and_none_equals_the_empty_string() -> None:
    h = setup(existing_run(text="", summary=f"{MARKDOWN_SUMMARY}\n"))

    result = run_check(h)

    assert result.ok
    assert result.message == "check run 'lock files' on 0123456 is already up to date"
    assert result.url == f"{URL}5"
    assert dict(result.outputs) == {"decision": "unchanged", "id": "5"}
    assert writes(h) == []


def test_a_details_url_of_githubs_own_does_not_cause_a_rewrite() -> None:
    h = setup(existing_run(details_url="https://github.com/apps/fake-app"))

    assert dict(run_check(h).outputs)["decision"] == "unchanged"
    assert writes(h) == []


def test_unchanged_with_details_url_and_external_id_when_they_match() -> None:
    h = setup(existing_run(details_url="https://example.com/x", external_id="id-1"))

    result = run_check(h, details_url="https://example.com/x", external_id="id-1")

    assert dict(result.outputs)["decision"] == "unchanged"


@pytest.mark.parametrize(
    ("change", "args"),
    [
        ({"status": "in_progress", "conclusion": None}, {}),
        ({"conclusion": "failure"}, {}),
        ({"title": "Older title"}, {}),
        ({"summary": "older"}, {}),
        ({"text": "older"}, {}),
        ({}, {"text": "new details"}),
        ({}, {"details_url": "https://example.com/new"}),
        ({"details_url": "https://example.com/old"}, {"details_url": "https://example.com/new"}),
        ({}, {"external_id": "new"}),
    ],
)
def test_updates_the_check_run_when_something_differs(
    change: dict[str, Any], args: dict[str, object]
) -> None:
    h = setup(existing_run(**change))

    result = run_check(h, **args)

    assert result.message == "updated check run 'lock files' on 0123456 (success)"
    assert result.url == f"{URL}5"
    assert dict(result.outputs) == {"decision": "updated", "id": "5"}
    assert len(writes(h)) == 1
    kind, run_id, payload = writes(h)[0]
    assert (kind, run_id) == ("update", 5)
    assert payload["status"] == "completed"
    assert payload["conclusion"] == "success"
    assert payload["title"] == "Lock files"
    assert payload["summary"] == MARKDOWN_SUMMARY
    assert [name for name, _ in h.factory.created[1].calls] == [
        "list_check_runs",
        "update_check_run",
    ]


def test_an_update_without_text_clears_the_text_the_run_had() -> None:
    h = setup(existing_run(text="old details"))

    run_check(h)

    assert writes(h)[0][2]["text"] == ""
    assert h.state.check_runs[SHA][0].text == ""


def test_a_check_run_of_another_app_with_the_same_name_is_never_touched() -> None:
    other = existing_run(3, app_id=99, app_slug="other-app", title="theirs")
    h = setup(other)

    result = run_check(h)

    assert dict(result.outputs) == {"decision": "created", "id": "4"}
    assert [kind for kind, _, _ in writes(h)] == ["create"]
    assert h.state.check_runs[SHA][0] == other


def test_a_run_without_an_app_is_not_ours() -> None:
    h = setup(existing_run(3, app_id=None, app_slug=""))

    assert dict(run_check(h).outputs)["decision"] == "created"


def test_a_check_run_with_another_name_is_not_a_match() -> None:
    h = setup(existing_run(3, name="other check"))

    assert dict(run_check(h).outputs)["decision"] == "created"


def test_with_several_matches_the_highest_id_is_updated() -> None:
    h = setup(
        existing_run(2, title="old"),
        existing_run(9, title="newer"),
        existing_run(4, title="middle"),
    )

    result = run_check(h)

    assert dict(result.outputs) == {"decision": "updated", "id": "9"}
    assert writes(h)[0][1] == 9


def test_without_a_known_bot_login_nothing_is_written() -> None:
    h = Harness(FakeRepository(), actor=None, policy=LOCKFILES)

    result = run_check(h)

    assert result.outcome is VerbOutcome.ERROR
    assert "did not report the bot's login" in result.message
    assert dict(result.outputs) == {"decision": "error"}
    assert h.factory.created[1].calls == []


# -- validation -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"sha": ""}, "--sha '' is not a full commit id"),
        ({"sha": "0123456"}, "is not a full commit id"),
        ({"sha": SHA.upper()}, "is not a full commit id"),
        ({"sha": SHA + "0"}, "is not a full commit id"),
        ({"name": ""}, "--name is required and must be one line"),
        ({"name": "  "}, "--name is required and must be one line"),
        ({"name": "a\nb"}, "--name is required and must be one line"),
        ({"name": "x" * 101}, "--name must be at most 100 characters"),
        ({"title": ""}, "--title is required and must be one line"),
        ({"title": "a\rb"}, "--title is required and must be one line"),
        ({"title": "x" * 256}, "--title must be at most 255 characters"),
        ({"summary": ""}, "--summary is required and must not be empty"),
        ({"summary": " \n "}, "--summary is required and must not be empty"),
        ({"summary": "x" * 65536}, "--summary must be at most 65535 characters"),
        ({"text": "x" * 65536}, "--text must be at most 65535 characters"),
        ({"status": "done"}, "--status 'done' must be one of queued, in_progress, completed"),
        ({"conclusion": "great"}, "--conclusion 'great' must be one of success"),
        ({"conclusion": None}, "--conclusion is required when the status is completed"),
        (
            {"status": "in_progress"},
            "--conclusion must not be given when the status is in_progress",
        ),
        (
            {"status": "queued"},
            "--conclusion must not be given when the status is queued",
        ),
        ({"details_url": "http://example.com"}, "--details-url must be an https:// URL"),
        ({"details_url": "https://exa mple.com"}, "--details-url must be an https:// URL"),
        (
            {"conclusion": "action_required"},
            "--details-url is required when the conclusion is action_required",
        ),
        ({"external_id": "a b"}, "--external-id may only contain"),
        ({"external_id": "x" * 65}, "--external-id may only contain"),
    ],
)
def test_refuses_bad_arguments_before_any_call(args: dict[str, object], message: str) -> None:
    h = setup()

    result = run_check(h, **args)

    assert result.outcome is VerbOutcome.ERROR
    assert message in result.message
    assert dict(result.outputs) == {"decision": "error"}
    assert writes(h) == []
    assert h.factory.created[1].calls == []  # the verb's client was never used


def test_a_missing_required_argument_is_refused() -> None:
    h = setup()
    request = VerbRequest("acme", "widgets", "lockfiles", Verb.CHECK, args={"sha": SHA})

    result = h.gate.run(request)

    assert result.outcome is VerbOutcome.ERROR
    assert "--name is required" in result.message


def test_the_maximum_lengths_are_accepted() -> None:
    h = setup()

    result = run_check(h, name="n" * 100, title="t" * 255, external_id="e" * 64)

    assert result.ok


# -- annotations ------------------------------------------------------------------------------


def test_annotations_are_sent_in_batches_of_fifty(tmp_path: Path) -> None:
    h = setup()

    result = run_check(h, annotations_file=write_annotations(tmp_path, 120))

    assert dict(result.outputs) == {"decision": "created", "id": "1"}
    kinds = [(kind, run_id, len(payload["annotations"])) for kind, run_id, payload in writes(h)]
    assert kinds == [("create", 1, 50), ("update", 1, 50), ("update", 1, 20)]
    first = writes(h)[0][2]["annotations"]
    assert [entry["message"] for entry in first] == [f"problem {i}" for i in range(50)]
    for _, _, payload in writes(h)[1:]:
        assert set(payload) == {"title", "summary", "annotations"}
        assert payload["title"] == "Lock files"
        assert payload["summary"] == MARKDOWN_SUMMARY
    assert writes(h)[1][2]["annotations"][0]["message"] == "problem 50"
    assert writes(h)[2][2]["annotations"][-1]["message"] == "problem 119"


def test_annotations_on_an_existing_run_always_update_it(tmp_path: Path) -> None:
    h = setup(existing_run())

    result = run_check(h, annotations_file=write_annotations(tmp_path, 3))

    assert dict(result.outputs) == {"decision": "updated", "id": "5"}
    assert len(writes(h)) == 1
    assert len(writes(h)[0][2]["annotations"]) == 3


def test_an_existing_run_gets_the_first_batch_in_the_update_and_the_rest_after(
    tmp_path: Path,
) -> None:
    h = setup(existing_run())

    run_check(h, annotations_file=write_annotations(tmp_path, 51))

    assert [(kind, len(payload["annotations"])) for kind, _, payload in writes(h)] == [
        ("update", 50),
        ("update", 1),
    ]


def test_exactly_fifty_annotations_need_no_extra_update(tmp_path: Path) -> None:
    h = setup()

    run_check(h, annotations_file=write_annotations(tmp_path, 50))

    assert len(writes(h)) == 1


def test_five_hundred_annotations_are_the_limit(tmp_path: Path) -> None:
    ok = setup()
    too_many = setup()

    accepted = run_check(ok, annotations_file=write_annotations(tmp_path, 500))
    refused = run_check(too_many, annotations_file=write_annotations(tmp_path, 501))

    assert accepted.ok
    assert len(writes(ok)) == 10
    assert refused.outcome is VerbOutcome.ERROR
    assert "501 annotations, at most 500" in refused.message
    assert writes(too_many) == []
    assert too_many.factory.created[1].calls == []


def test_an_empty_annotations_array_is_the_same_as_none(tmp_path: Path) -> None:
    h = setup(existing_run())

    result = run_check(h, annotations_file=annotations_file(tmp_path, []))

    assert dict(result.outputs)["decision"] == "unchanged"


def test_annotation_fields_are_normalised(tmp_path: Path) -> None:
    h = setup()
    entry = annotation(message="  look  ", title=" T ")

    run_check(h, annotations_file=annotations_file(tmp_path, [entry]))

    sent = writes(h)[0][2]["annotations"][0]
    assert sent == {**entry, "message": "look", "title": "T"}


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("text", "annotation 0 is not an object"),
        (annotation(extra=1), "annotation 0, field 'extra': unknown field"),
        (annotation(path=""), "annotation 0, field 'path'"),
        (annotation(path=5), "annotation 0, field 'path'"),
        (annotation(path="/etc/passwd"), "annotation 0, field 'path'"),
        (annotation(path="a/../b"), "annotation 0, field 'path'"),
        (annotation(start_line=0), "annotation 0, field 'start_line'"),
        (annotation(start_line="3"), "annotation 0, field 'start_line'"),
        (annotation(start_line=True), "annotation 0, field 'start_line'"),
        (annotation(end_line=1.5), "annotation 0, field 'end_line'"),
        (annotation(end_line=2), "annotation 0, field 'end_line': must not be smaller"),
        (annotation(annotation_level="info"), "annotation 0, field 'annotation_level'"),
        (annotation(message=""), "annotation 0, field 'message'"),
        (annotation(message=" "), "annotation 0, field 'message'"),
        (annotation(message=7), "annotation 0, field 'message'"),
        (annotation(message="m" * 1001), "annotation 0, field 'message': must be at most 1000"),
        (annotation(title="a\nb"), "annotation 0, field 'title': must be a one line string"),
        (annotation(title=3), "annotation 0, field 'title'"),
        (annotation(title="t" * 256), "annotation 0, field 'title': must be at most 255"),
    ],
)
def test_annotation_problems_name_the_index_and_the_field(
    tmp_path: Path, entry: object, message: str
) -> None:
    h = setup()
    entries = [annotation(), entry]
    expected = message.replace("annotation 0", "annotation 1")

    result = run_check(h, annotations_file=annotations_file(tmp_path, entries))

    assert result.outcome is VerbOutcome.ERROR
    assert expected in result.message
    assert "--annotations-file" in result.message
    assert writes(h) == []
    assert h.factory.created[1].calls == []


def test_the_longest_annotation_message_and_title_are_accepted(tmp_path: Path) -> None:
    h = setup()
    entry = annotation(message="m" * 1000, title="t" * 255)

    assert run_check(h, annotations_file=annotations_file(tmp_path, [entry])).ok


def test_an_unreadable_annotations_file_is_refused(tmp_path: Path) -> None:
    result = run_check(setup(), annotations_file=str(tmp_path / "missing.json"))

    assert result.outcome is VerbOutcome.ERROR
    assert "--annotations-file: cannot read" in result.message


def test_an_annotations_file_that_is_not_json_is_refused(tmp_path: Path) -> None:
    result = run_check(setup(), annotations_file=annotations_file(tmp_path, "{not json"))

    assert result.outcome is VerbOutcome.ERROR
    assert "is not valid JSON" in result.message


def test_an_annotations_file_that_is_not_an_array_is_refused(tmp_path: Path) -> None:
    result = run_check(setup(), annotations_file=annotations_file(tmp_path, {"path": "a"}))

    assert result.outcome is VerbOutcome.ERROR
    assert "must hold a JSON array" in result.message


# -- policy -----------------------------------------------------------------------------------


@pytest.mark.parametrize("module", ["lockfiles", "release", "sync"])
def test_the_modules_that_may_run_check(module: str) -> None:
    policy = f"version: 1\nmodules:\n  {module}:\n    enabled: true\n"
    if module == "sync":
        policy += "    paths: [deploy]\n"
    h = Harness(FakeRepository(), policy=policy)
    args: dict[str, object] = {
        "sha": SHA,
        "name": "n",
        "title": "t",
        "summary": "s",
        "conclusion": "success",
    }

    result = h.run(Verb.CHECK, args, module=module)

    assert result.ok, result.message


def test_the_check_subcommand_parses_routes_and_reports_the_decision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    h = setup()
    monkeypatch.setattr("lktec_bot.cli.build_gate", lambda env=None: h.gate)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output.txt"))
    summary = tmp_path / "summary-body.md"
    summary.write_text("From a file.\n", encoding="utf-8")
    command = [
        "check",
        "-R",
        "acme/widgets",
        "-m",
        "lockfiles",
        "--sha",
        SHA,
        "--name",
        "lock files",
        "--title",
        "Lock files",
        "--summary-file",
        str(summary),
        "--text",
        "details",
        "--status",
        "completed",
        "--conclusion",
        "neutral",
        "--details-url",
        "https://example.com/run",
        "--external-id",
        "run-1",
        "--annotations-file",
        write_annotations(tmp_path, 2),
    ]

    first = CliRunner().invoke(app, command)

    assert first.exit_code == 0, first.output
    assert "ok: created check run 'lock files' on 0123456 (neutral)" in first.output
    created = writes(h)[0][2]
    assert created["summary"] == "From a file."
    assert created["text"] == "details"
    assert created["conclusion"] == "neutral"
    assert created["details_url"] == "https://example.com/run"
    assert created["external_id"] == "run-1"
    assert len(created["annotations"]) == 2
    values = [
        line.split("=", 1)
        for line in (tmp_path / "output.txt").read_text(encoding="utf-8").splitlines()
    ]
    assert ["decision", "created"] in values
    assert ["id", "1"] in values


def test_the_check_subcommand_reports_a_denial_as_the_decision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    h = Harness(FakeRepository())
    monkeypatch.setattr("lktec_bot.cli.build_gate", lambda env=None: h.gate)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output.txt"))

    command = ["check", "-R", "acme/widgets", "-m", "automerge", "--sha", SHA, "--name", "n"]
    command += ["--title", "t", "--summary", "s", "--conclusion", "success"]

    result = CliRunner().invoke(app, command)

    assert result.exit_code == 3
    lines = (tmp_path / "output.txt").read_text(encoding="utf-8").splitlines()
    assert "decision=denied" in lines


def test_the_check_subcommand_needs_a_summary_and_a_valid_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = setup()
    monkeypatch.setattr("lktec_bot.cli.build_gate", lambda env=None: h.gate)
    common = ["check", "-R", "acme/widgets", "-m", "lockfiles", "--sha", SHA]
    common += ["--name", "n", "--title", "t", "--conclusion", "success"]

    no_summary = CliRunner().invoke(app, common)
    both = CliRunner().invoke(app, [*common, "--summary", "s", "--summary-file", "x"])
    bad_status = CliRunner().invoke(app, [*common, "--summary", "s", "--status", "done"])

    assert no_summary.exit_code == 2
    assert "--summary or --summary-file is required" in no_summary.output
    assert both.exit_code == 2
    assert "either --summary or --summary-file" in both.output
    assert bad_status.exit_code == 2
    assert writes(h) == []


def test_check_is_denied_for_the_modules_that_do_not_list_it() -> None:
    h = Harness(FakeRepository())

    result = h.gate.run(VerbRequest("acme", "widgets", "automerge", Verb.CHECK, args={}))

    assert result.outcome is VerbOutcome.DENIED
    assert "may not run 'check'" in result.message
