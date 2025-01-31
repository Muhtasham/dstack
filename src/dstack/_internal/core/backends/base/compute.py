import os
from abc import ABC, abstractmethod
from typing import List, Optional

import yaml

from dstack import version
from dstack._internal.core.models.backends.base import BackendType
from dstack._internal.core.models.instances import (
    InstanceOfferWithAvailability,
    InstanceState,
    LaunchedGatewayInfo,
    LaunchedInstanceInfo,
)
from dstack._internal.core.models.runs import Job, Requirements, Run


class Compute(ABC):
    @abstractmethod
    def get_offers(
        self, requirements: Optional[Requirements] = None
    ) -> List[InstanceOfferWithAvailability]:
        pass

    @abstractmethod
    def run_job(
        self,
        run: Run,
        job: Job,
        instance_offer: InstanceOfferWithAvailability,
        project_ssh_public_key: str,
        project_ssh_private_key: str,
    ) -> LaunchedInstanceInfo:
        pass

    @abstractmethod
    def terminate_instance(self, instance_id: str, region: str):
        pass

    def get_instance_state(self, instance_id: str, region: str) -> InstanceState:
        pass

    def create_gateway(
        self,
        instance_name: str,
        ssh_key_pub: str,
        region: str,
        project_id: str,
    ) -> LaunchedGatewayInfo:
        raise NotImplementedError()


def get_user_data(
    backend: BackendType, image_name: str, authorized_keys: List[str], registry_auth_required: bool
) -> str:
    commands = get_shim_commands(
        backend=backend,
        image_name=image_name,
        authorized_keys=authorized_keys,
        registry_auth_required=registry_auth_required,
    )
    return get_cloud_config(
        runcmd=[["sh", "-c", " && ".join(commands)]],
        ssh_authorized_keys=authorized_keys,
    )


def get_shim_commands(
    backend: BackendType,
    image_name: str,
    authorized_keys: List[str],
    registry_auth_required: bool,
) -> List[str]:
    build = get_dstack_runner_version()
    env = {
        "DSTACK_BACKEND": backend.value,
        "DSTACK_RUNNER_LOG_LEVEL": "6",
        "DSTACK_RUNNER_VERSION": build,
        "DSTACK_IMAGE_NAME": image_name,
        "DSTACK_PUBLIC_SSH_KEY": "\n".join(authorized_keys),
        "DSTACK_HOME": "/root/.dstack",
    }
    commands = get_dstack_shim(build)
    for k, v in env.items():
        commands += [f'export "{k}={v}"']
    commands += get_run_shim_script(registry_auth_required)
    return commands


def get_dstack_runner_version() -> str:
    if version.__is_release__:
        return version.__version__
    return os.environ.get("DSTACK_RUNNER_VERSION", None) or "latest"


def get_cloud_config(**config) -> str:
    return "#cloud-config\n" + yaml.dump(config, default_flow_style=False)


def get_dstack_shim(build: str) -> List[str]:
    bucket = "dstack-runner-downloads-stgn"
    if version.__is_release__:
        bucket = "dstack-runner-downloads"

    return [
        f'sudo curl --output /usr/local/bin/dstack-shim "https://{bucket}.s3.eu-west-1.amazonaws.com/{build}/binaries/dstack-shim-linux-amd64"',
        "sudo chmod +x /usr/local/bin/dstack-shim",
    ]


def get_run_shim_script(registry_auth_required: bool) -> List[str]:
    dev_flag = "" if version.__is_release__ else "--dev"
    with_auth_flag = "--with-auth" if registry_auth_required else ""
    return [
        f"nohup dstack-shim {dev_flag} docker {with_auth_flag} --keep-container >/root/shim.log 2>&1 &"
    ]


def get_gateway_user_data(authorized_key: str) -> str:
    return get_cloud_config(
        package_update=True,
        packages=["nginx"],
        snap={"commands": [["install", "--classic", "certbot"]]},
        runcmd=[["ln", "-s", "/snap/bin/certbot", "/usr/bin/certbot"]],
        ssh_authorized_keys=[authorized_key],
        users=[
            "default",
            {
                "name": "www-data",
                "ssh_authorized_keys": [authorized_key],
            },
        ],
    )
