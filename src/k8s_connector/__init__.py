from pathlib import Path

from common_libs.logging import setup_logging

setup_logging(Path(__file__).parent.parent / "cfg" / "logging.yaml")


from .k8s import K8sConnector  # noqa: E402
