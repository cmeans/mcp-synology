"""The vdsm container's network, per container engine (no container is started)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.vdsm import container as container_module


class _FakeContainer:
    """Records what VirtualDsmContainer.start() asks for."""

    last_kwargs: dict[str, Any]

    def __init__(self, image: str) -> None:
        self.image = image

    def with_env(self, *_: Any) -> None:
        pass

    def with_exposed_ports(self, *_: Any) -> None:
        pass

    def with_volume_mapping(self, *_: Any, **__: Any) -> None:
        pass

    def with_kwargs(self, **kwargs: Any) -> None:
        type(self).last_kwargs = kwargs

    def start(self) -> None:
        pass


@pytest.mark.parametrize(
    "docker_host",
    ["unix:///run/user/1000/podman/podman.sock", "unix:///var/run/docker.sock"],
)
def test_no_engine_gets_a_network_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, docker_host: str
) -> None:
    """Podman 6 refuses the slirp4netns this used to ask for (#537), and each engine's default
    network works (#540)."""
    monkeypatch.setenv("DOCKER_HOST", docker_host)
    monkeypatch.setenv("TESTCONTAINERS_RYUK_DISABLED", "true")  # start() would set it for good
    monkeypatch.setattr(container_module, "DockerContainer", _FakeContainer)
    monkeypatch.setattr(container_module.VirtualDsmContainer, "_wait_for_dsm", lambda _self: None)

    version = next(iter(container_module.DSM_VERSIONS))
    container_module.VirtualDsmContainer(version, tmp_path / "storage").start()

    assert "network_mode" not in _FakeContainer.last_kwargs
