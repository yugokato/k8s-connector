from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    __version__ = version("k8s-connector")
except PackageNotFoundError:
    pass


import yaml
from common_libs.logging import setup_logging

setup_logging(yaml.safe_load((Path(__file__).parent.parent / "cfg" / "logging.yaml").read_text(encoding="utf-8")))


from .k8s import K8sConnector  # noqa: E402
