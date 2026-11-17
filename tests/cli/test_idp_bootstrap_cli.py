"""`atlas-admin idp-bootstrap`: arguments, exit codes, signals and the output it prints
(PLAN Phase 5b-2b). The engine's steps are in tests/kernel/identity/test_bootstrap.py."""

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from atlas.kernel.cli import build_parser, main
from atlas.kernel.cli.idp import _cert_pem, _new_ca
from atlas.kernel.cli.idp_bootstrap import (
    DEFAULT_CA_FILE,
    DEFAULT_CLIENTS_DIR,
    DEFAULT_IDP_URL,
    DEFAULT_SOCKET,
    DEFAULT_STATE_DIR,
    ROTATED_HINT,
    _run,
)
from atlas.kernel.identity.bootstrap import BootstrapError, BootstrapResult

ENV = {
    "ATLAS_PUBLIC_URL": "https://atlas.home.arpa",
    "ATLAS_IDP_PUBLIC_URL": "https://id.home.arpa",
}


class FakeBootstrap:
    def __init__(
        self, outcome: object = BootstrapResult(skipped=False, token_rotated=False)
    ) -> None:
        self.outcome = outcome
        self.cleaned_up = False

    async def run(self) -> BootstrapResult:
        try:
            if isinstance(self.outcome, BaseException):
                raise self.outcome
            if self.outcome == "wait":
                await asyncio.sleep(30)
            assert isinstance(self.outcome, BootstrapResult)
            return self.outcome
        finally:
            self.cleaned_up = True  # stands in for the DisableAccount of the engine


def run_cli(bootstrap: Any) -> int:
    return asyncio.run(_run(bootstrap))


# --- the command ---------------------------------------------------------------------


def test_the_command_and_its_defaults() -> None:
    args = build_parser().parse_args(["idp-bootstrap"])
    assert (args.verify, args.rotate_console_token) == (False, False)
    assert args.idp_url == DEFAULT_IDP_URL == "https://idp:8443"
    assert args.ca_file == DEFAULT_CA_FILE
    assert args.socket == DEFAULT_SOCKET
    assert args.state_dir == DEFAULT_STATE_DIR and args.clients_dir == DEFAULT_CLIENTS_DIR
    # The defaults are the mounts of docker-compose.yml's atlas-idp-bootstrap service.
    compose = (Path(__file__).resolve().parents[2] / "deploy" / "docker-compose.yml").read_text()
    for path in (
        DEFAULT_CA_FILE.parent,
        DEFAULT_SOCKET.parent,
        DEFAULT_STATE_DIR,
        DEFAULT_CLIENTS_DIR,
    ):
        assert f":{path}" in compose, path
    flags = build_parser().parse_args(["idp-bootstrap", "--verify", "--rotate-console-token"])
    assert (flags.verify, flags.rotate_console_token) == (True, True)


def test_missing_settings_are_a_clear_exit_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["idp-bootstrap"]) == 2
    err = capsys.readouterr().err
    assert "ATLAS_PUBLIC_URL: is required" in err and "ATLAS_IDP_PUBLIC_URL: is required" in err
    assert "Traceback" not in err


def test_missing_paths_are_a_clear_exit_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    base = ["idp-bootstrap", "--state-dir", str(tmp_path), "--clients-dir", str(tmp_path)]
    assert main([*base, "--ca-file", str(tmp_path / "no-ca.pem")]) == 2
    assert "CA certificate" in capsys.readouterr().err
    (tmp_path / "ca.pem").write_text(_cert_pem(_new_ca()[1]))
    args = [*base, "--ca-file", str(tmp_path / "ca.pem"), "--socket", str(tmp_path / "none.sock")]
    assert main(args) == 2
    assert "admin socket" in capsys.readouterr().err
    (tmp_path / "state").mkdir()
    sock = tmp_path / "x.sock"
    sock.write_text("")
    args = ["idp-bootstrap", "--ca-file", str(tmp_path / "ca.pem"), "--socket", str(sock)]
    assert main([*args, "--state-dir", str(tmp_path / "nope"), "--clients-dir", str(tmp_path)]) == 2
    assert "state directory" in capsys.readouterr().err


def test_an_unreachable_idp_fails_with_a_step_and_exit_1_and_no_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The whole wiring with nothing behind the socket: RecoverAccount cannot be sent.
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    (tmp_path / "ca.pem").write_text(_cert_pem(_new_ca()[1]))
    state, clients = tmp_path / "state", tmp_path / "clients"
    state.mkdir()
    clients.mkdir()
    dead_socket = tmp_path / "dead.sock"
    dead_socket.write_text("")  # exists, but nothing listens
    code = main(
        [
            "idp-bootstrap",
            "--ca-file",
            str(tmp_path / "ca.pem"),
            "--socket",
            str(dead_socket),
            "--state-dir",
            str(state),
            "--clients-dir",
            str(clients),
        ]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "atlas-admin idp-bootstrap: failed at step recover:" in captured.err
    assert "Traceback" not in captured.err + captured.out
    # The disable was attempted too, and its failure said out loud.
    assert "WARNING idm_admin could not be disabled" in captured.out
    # The record says the run did not complete, so atlas-api would not start on it.
    assert '"complete": false' in (state / "bootstrap.json").read_text()


# --- exit codes and signals ----------------------------------------------------------


def test_success_is_exit_0_and_a_rotation_says_to_restart_atlas_api(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_cli(FakeBootstrap(BootstrapResult(skipped=False, token_rotated=False))) == 0
    assert ROTATED_HINT not in capsys.readouterr().out
    assert run_cli(FakeBootstrap(BootstrapResult(skipped=False, token_rotated=True))) == 0
    assert ROTATED_HINT in capsys.readouterr().out
    assert "restart atlas-api" in ROTATED_HINT


def test_a_failed_step_is_exit_1_and_names_the_step(capsys: pytest.CaptureFixture[str]) -> None:
    bootstrap = FakeBootstrap(BootstrapError("client-atlas", "Kanidm gave no secret"))
    assert run_cli(bootstrap) == 1
    err = capsys.readouterr().err
    assert err.strip() == (
        "atlas-admin idp-bootstrap: failed at step client-atlas: Kanidm gave no secret"
    )
    assert bootstrap.cleaned_up is True


def test_an_unexpected_error_prints_its_type_and_never_its_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    text = "text-that-could-hold-a-credential"
    assert run_cli(FakeBootstrap(RuntimeError(text))) == 1
    err = capsys.readouterr().err
    assert "unexpected RuntimeError" in err and text not in err


@pytest.mark.parametrize(("number", "code"), [(signal.SIGTERM, 143), (signal.SIGINT, 130)])
def test_a_signal_cancels_the_run_once_and_the_cleanup_still_happens(
    number: signal.Signals, code: int, capsys: pytest.CaptureFixture[str]
) -> None:
    bootstrap = FakeBootstrap("wait")

    async def scenario() -> int:
        loop = asyncio.get_running_loop()
        # Two signals in a row: the second one must not interrupt the cleanup.
        loop.call_later(0.05, os.kill, os.getpid(), number)
        loop.call_later(0.06, os.kill, os.getpid(), number)
        return await _run(bootstrap)

    assert asyncio.run(scenario()) == code
    assert bootstrap.cleaned_up is True
    assert f"stopped by signal {int(number)}" in capsys.readouterr().err
