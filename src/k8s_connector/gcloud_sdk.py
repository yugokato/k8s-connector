import json
from pathlib import Path

from common_libs.containers.container import BaseContainer, requires_container
from common_libs.logging import get_logger

logger = get_logger(__name__)


class GCloudSDKContainer(BaseContainer):
    """GCloud SDK container

    https://hub.docker.com/r/google/cloud-sdk/
    """

    def __init__(
        self,
        *,
        tag: str = "latest",
        service_account_file_path: Path | str,
        run: bool = True,
        timeout: int = 60,
        **kwargs,
    ):
        self.service_account_file_path = Path(service_account_file_path)
        self.project = json.loads(self.service_account_file_path.read_text())["project_id"]
        super().__init__(
            "google/cloud-sdk", tag=tag, labels={"project": self.project, "version": "1.0.0"}, timeout=timeout, **kwargs
        )
        self.service_account_file_path_container = f"{self.tmp_dir}/{self.service_account_file_path.name}"
        self.reused = False

        if run:
            self.run()

    def run(self, *args, **kwargs):
        """Start a container. If existing container eixsts, reuse it"""
        if existing_containers := self.get_existing_containers():
            existing_container = existing_containers[0]
            logger.info(f"Reusing existing {self.image}:{self.tag} container: {existing_container.id}")
            self.container = existing_container
            self.reused = True
        else:
            self.reused = False
            super().run(
                volumes={
                    str(self.service_account_file_path): {
                        "bind": self.service_account_file_path_container,
                        "mode": "ro",
                    }
                },
                environment={"USE_GKE_GCLOUD_AUTH_PLUGIN": "true"},
            )
            self.setup()

    @requires_container
    def setup(self):
        """Activate Service Account"""
        logger.info("Activating Service Account credentials...")
        self.exec_run(
            f"gcloud auth activate-service-account --key-file={self.service_account_file_path_container} "
            f"--project {self.project}"
        )
