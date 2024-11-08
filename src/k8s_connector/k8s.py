from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from functools import cached_property, lru_cache, partial, wraps
from pathlib import Path
from typing import Any

import dateparser
import yaml
from common_libs.ansi_colors import remove_color_code
from common_libs.containers.container import requires_container
from common_libs.containers.utils.log_parser import parse_json_logs, parse_streamed_json_logs, parse_streamed_logs
from common_libs.containers.utils.output_parser import parse_table_output
from common_libs.lock import Lock
from common_libs.logging import get_logger
from common_libs.utils import list_items, wait_until

from .gcloud_sdk import GCloudSDKContainer

logger = get_logger(__name__)


class K8sApp(StrEnum):
    # Define your apps here. You can check the list of our apps/namespaces in the output of `<K8sConnector>.get_pods()`.
    # This is an example
    APP1 = "app1"


class K8sNamespace(StrEnum):
    # Define your namespaces here. You can check the list of namespaces in `<K8sConnector>.get_namespaces()`.
    # This is an example
    EXAMPLE = "example"


@dataclass
class K8sAppContext:
    app: K8sApp
    namespace: K8sNamespace = field(init=False)

    def __post_init__(self):
        if not isinstance(self.app, K8sApp):
            self.app = K8sApp(self.app)
        if self.app not in APP_NAMESPACE_MAP:
            raise NotImplementedError(f"Namespace for app '{self.app}' needs to be defined")
        self.namespace = APP_NAMESPACE_MAP[self.app]


APP_NAMESPACE_MAP = {
    # Map your apps and namespaces here.
    # This is an example
    K8sApp.APP1: K8sNamespace.EXAMPLE,
}
APP_JSON_LOGGING_FORMATTER: dict[K8sApp, str] = {
    # Add custom formatters for apps that support JSON logging here, if needed.
    # This is an example
    K8sApp.APP1: "[{created}] [{levelname}]{{{request_id}}}[{logger_name}] - {message}"
}


def requires_jq(f):
    """A decorated function requires jq"""

    @wraps(f)
    def wrapper(self: K8sConnector, *args, **kwargs):
        self._install_jq()
        return f(self, *args, **kwargs)

    return wrapper


def restrict_unparsable_options(f):
    """Restrict unsupported options when parse=True is given"""

    @wraps(f)
    def wrapper(self: K8sConnector, *args, **kwargs):
        unsupported_options = ["grep", "grep_v", "highlight", "set_x", "pipes", "stream", "detach"]
        if kwargs.get("parse") and (unsupported_options_given := [o for o in unsupported_options if kwargs.get(o)]):
            raise ValueError(
                f"parse option does not support the following option(s):\n{list_items(unsupported_options_given)}"
            )
        return f(self, *args, **kwargs)

    return wrapper


class K8sConnector(GCloudSDKContainer):
    """GCloud SDK container for accessing your GKE cluster

    Usage:
        >>> k8s = K8sConnector(service_account_file_path="/path/to/service/account", cluster_name="name", zone="zone")
        >>> # get pods
        >>> pods = k8s.app1.get_pods()
        >>> # stream logs
        >>> k8s.app1.get_logs(follow=True)
    """

    IS_SETUP_DONE = False
    JQ_INSTALLED = False

    def __init__(
        self,
        service_account_file_path: Path | str,
        cluster_name: str,
        region: str = None,
        zone: str = None,
        app_label_selector_key: str = "app",
        run: bool = True,
        timeout: int = 60,
        **kwargs,
    ):
        if not region and not zone:
            raise ValueError("Either region or zone is required")

        super().__init__(service_account_file_path=service_account_file_path, run=False, timeout=timeout, **kwargs)
        self.cluster_name = cluster_name
        self.region = region
        self.zone = zone
        self.app_label_selector_key = app_label_selector_key
        self._app_context: K8sAppContext | None = None

        if run:
            self.run()

    def run(self):
        """Start the container and do setup"""
        super().run()

        # Use lock so that the setup is done only once when the container is used in threads
        with Lock("k8s_connector_setup"):
            if not self.reused:
                # Reset the existing env setup flag so that k8s env setup will always be triggered on the new container
                K8sConnector.IS_SETUP_DONE = False

            if not K8sConnector.IS_SETUP_DONE:
                self._setup_kubeconfig()
                K8sConnector.IS_SETUP_DONE = True

    @property
    def app_context(self) -> K8sAppContext:
        """Return the curernt app context"""
        if self._app_context is None:
            raise ValueError("App context is not set")

        return self._app_context

    @property
    def env_vars(self) -> dict[str, str]:
        """Return non-confidential env vars set as configmap"""
        jsonpath = '{.items[?(@.kind == "ConfigMap")].data}'
        cmd = f"kubectl get configmap {self._app_filtering_options} -o jsonpath='{jsonpath}'"
        _, output = self.exec_run(cmd, quiet=True)
        if output:
            return json.loads(output)
        else:
            return {}

    @lru_cache
    def with_app_context(self, app: K8sApp | str) -> K8sConnectorWithAppContext:
        """Return a k8s connector with the specified app context been set"""
        return K8sConnectorWithAppContext(self, app)

    @cached_property
    def app1(self):
        """Shortcut to the app1 app"""
        return self.with_app_context(K8sApp.APP1)

    @requires_container
    @restrict_unparsable_options
    def get_namespaces(self, parse: bool = False, **kwargs) -> str | list[dict[str, str]]:
        """Get output of 'kubectl get namespaces' command

        :param parse: Parse the raw output and return as a list of dictionaries
        :param kwargs: Any parameters supported in exec_run()
        """
        cmd = "kubectl get namespaces"
        _, output = self.exec_run(cmd, **kwargs)
        if parse:
            return parse_table_output(output)
        else:
            return output

    @requires_container
    @restrict_unparsable_options
    def get_pods(self, parse: bool = False, **kwargs) -> str | list[dict[str, str]]:
        """Get output of 'kubectl get pods' command for the current app/namespace, or for all apps/namespaces

        :param parse: Parse the raw output and return as a list of dictionaries
        :param kwargs: Any parameters supported in exec_run()
        """
        cmd = "kubectl get pods"
        if self._app_context:
            cmd += f" {self._app_filtering_options}"
        else:
            cmd += " -A"
        _, output = self.exec_run(cmd, **kwargs)
        if parse:
            return parse_table_output(output)
        else:
            return output

    @requires_container
    @restrict_unparsable_options
    def get_pod_details(self, pod_name: str, parse: bool = False, **kwargs) -> str | dict[str, str]:
        """Get output of 'kubectl get pod <pod name> -o yaml' command and return the output as a dictionary

        :param pod_name: Pod name
        :param parse: Parse the raw output and return as a dictionary
        :param kwargs: Any parameters supported in exec_run()
        """
        cmd = f"kubectl get pod {pod_name} -n {self.app_context.namespace} -o yaml"
        _, output = self.exec_run(cmd, **kwargs)
        if parse:
            return yaml.safe_load(output)
        else:
            return output

    @requires_container
    def describe_pods(self, pod_name: str = None, **kwargs) -> str:
        """Get output of 'kubectl describe pods' command for the current app

        :param pod_name: Pod name
        :param kwargs: Any parameters supported in exec_run()
        """
        if pod_name:
            cmd = f"kubectl describe pods {pod_name} -n {self.app_context.namespace}"
            _, output = self.exec_run(cmd, **kwargs)
            return output
        else:
            output = ""
            pods = self.get_pods(parse=True)
            for name in [pod["NAME"] for pod in pods]:
                output += self.describe_pods(name, **kwargs)
            return output

    @requires_container
    def get_logs(
        self,
        pod: str = None,
        container: str = None,
        follow: bool = False,
        limit_bytes: int = None,
        previous: bool = False,
        since: str = "30s",
        since_time: str = None,
        tail: int = -1,
        timestamps: bool = False,
        remove_color: bool = True,
        raw: bool = False,
        filters: dict[str, Any] = None,
        **kwargs,
    ) -> str:
        """Get output of 'kubectl logs' command for the current app, or stream logs.

        NOTE: since_time value will be parsed with dateparser library to support various format.
              If the value doesn't have a timezone, we will assum it as UTC

        :param pod: Pod name
        :param remove_color: Remove color code from the returned logs. This option will not affect the console
                             output, and not applicable when streaming logs
        :param raw: Return raw JSON logs. Defaults to return formatted logs by applying a pre-defined formatter, if it
                    is defined.
                    Applicable only for apps that supports JSON logging
        :param filters: Filter JSON logs with specified key/value pairs where each key is a JSON log filed name
                        and the value is a match condition. Applicable only for apps that supports JSON logging.
                        The following format is supported for each value:
                        - A regex pattern (Must be a re.compile() obj)
                        - A string with "*" where "*" matches anything
                        - A relational operation operator for integer values. eg. {"level": "<7"}
                        - Negation using "NOT". eg. {"foo": "NOT bar"}
                        - Anything else (check exact match)
                        NOTE: When multiple fields are given, the filter works as AND.
        :param kwargs: Any parameters supported in exec_run()

        See https://jamesdefabia.github.io/docs/user-guide/kubectl/kubectl_logs/ for other options

        """
        if self.app_context.app in APP_JSON_LOGGING_FORMATTER:
            if raw:
                log_formatter = None
            else:
                log_formatter = APP_JSON_LOGGING_FORMATTER[self.app_context.app]
            if follow:
                log_parser = partial(parse_streamed_json_logs, filters=filters, formatter=log_formatter)
            else:
                log_parser = partial(parse_json_logs, filters=filters, formatter=log_formatter)
        else:
            if raw or filters:
                raise NotImplementedError(
                    f"'raw' or 'filters' options are not applicable to {self.app_context.app} app"
                )
            if follow:
                log_parser = parse_streamed_logs
            else:
                log_parser = None

        cmd = "kubectl logs"
        if pod:
            cmd += f" {pod} -n {self.app_context.namespace}"
        else:
            cmd += f" {self._app_filtering_options}"
        if container:
            cmd += f" --container={container}"
        if limit_bytes:
            cmd += f" --limit-bytes={limit_bytes}"
        if previous:
            cmd += " --previous=true"
        if since_time:
            dt = dateparser.parse(since_time, settings={"TIMEZONE": "UTC", "RETURN_AS_TIMEZONE_AWARE": True})
            _since_time = datetime.strftime(dt, "%Y-%m-%dT%H:%M:%S%z")
            cmd += f' --since-time="{_since_time[:-2]}:{_since_time[-2:]}"'
        elif since:
            cmd += f" --since={since}"
        if tail is not None:
            cmd += f" --tail={tail}"
        if timestamps:
            cmd += " --timestamps=true"
        if follow:
            cmd += " -f --max-log-requests=30"
            self.exec_run(cmd, stream=True, output_parser=log_parser, **kwargs)
        else:
            _, output = self.exec_run(cmd, output_parser=log_parser, **kwargs)
            if remove_color:
                output = remove_color_code(output)
            return output

    @requires_container
    @restrict_unparsable_options
    @requires_jq
    def get_configmap_data(self, parse: bool = False, **kwargs) -> str | dict[str, str]:
        """Get filtered output of 'kubectl get configmap' command

        :param parse: Parse the raw output and return as a dictionary
        :param kwargs: Any parameters supported in exec_run()
        """
        cmd = (
            f"kubectl get configmap {self._app_filtering_options} -o json | "
            f"jq '.items[] | select(.kind == \"ConfigMap\") | .data'"
        )
        _, output = self.exec_run(cmd, **kwargs)
        if parse:
            return json.loads(output)
        else:
            return output

    @requires_container
    @restrict_unparsable_options
    def get_events(self, parse: bool = False, **kwargs) -> str | list[dict[str, str]]:
        """Get output of 'kubectl get events' command for the current namespace, or all namespaces

        :param parse: Parse the raw output and return as a list of dictionaries
        :param kwargs: Any parameters supported in exec_run()
        """
        cmd = "kubectl get events --sort-by=.metadata.creationTimestamp"
        if self._app_context:
            cmd += f" -n {self.app_context.namespace}"
        else:
            cmd += " -A"
        _, output = self.exec_run(cmd, **kwargs)
        if parse:
            if "No resources found" in output:
                return []
            else:
                return parse_table_output(output)
        else:
            return output

    @requires_container
    @restrict_unparsable_options
    def top(self, parse: bool = False, sort_by: str = "cpu", **kwargs) -> str | list[dict[str, str]]:
        """Get output of 'kubectl top pod --containers' command

        :param parse: Parse the raw output and return as a list of dictionaries
        :param sort_by: Sort by
        :param kwargs: Any parameters supported in exec_run()
        """
        cmd = f"kubectl top pod --containers --sort-by={sort_by} {self._app_filtering_options}"
        _, output = self.exec_run(cmd, **kwargs)
        if parse:
            return parse_table_output(output)
        else:
            return output

    @requires_container
    def wait_for_deployment(self):
        """Wait for the app deployment to start after successful build, and wait for it to complete"""
        self.wait_for_deployment_to_start()
        self.wait_for_deployment_to_complete()

    @requires_container
    def wait_for_deployment_to_start(self, timeout_sec: int = 300, thresholds_to_skip: int = 180):
        """Wait for deployment to start (=at least one container's status shows PodInitializing or Init:0/1)

        :param timeout_sec: Timeout in seconds
        :param thresholds_to_skip: Skip if all pods started within the specified thresholds, since this would
                                   mean deployment already happened recently. For now the threshold is
                                   180 seconds from the current time
        """

        def format_age(age_str: str) -> str:
            """Format pod AGE value to be something dateparser can parse

            eg.
                - "12h34m" -> "12h 34m"
                - "2d" -> "2 days"
            """
            pattern_d = r"(\d+)d"
            pattern_h_m = r"(\d+)h(\d+)m"
            if re.match(pattern_h_m, age_str):
                age = re.sub(pattern_h_m, r"\1h \2m", age_str)
            elif matched := re.match(pattern_d, age_str):
                age = f"{matched.group(1)} days"
            else:
                age = age_str
            return age

        def did_all_pods_start_within(seconds: int) -> bool:
            """Check whether all pods are started within the specified time in seconds"""
            pods = self.get_pods(parse=True, quiet=True)
            try:
                ages_str = [format_age(pod["AGE"]) for pod in pods]
                ages_dt = [
                    dateparser.parse(f"{x} ago", settings={"TIMEZONE": "UTC"}).replace(tzinfo=UTC) for x in ages_str
                ]
                dt_now = datetime.now(tz=UTC)
                seconds_elapsed_from_now = [(dt_now - x).total_seconds() for x in ages_dt]
            except Exception:
                logger.error(f"Failed to parse the output of 'kubectl get pods' command: {pods}")
                raise
            return all(x <= seconds for x in seconds_elapsed_from_now)

        def wait_for_pod_initialization_to_start() -> bool:
            pods = self.get_pods(parse=True, quiet=True)
            return any(pod["STATUS"] in ("PodInitializing", "Init:0/1") for pod in pods)

        logger.info(f"Waiting for {self.app_context.app} deployment to start...")
        if thresholds_to_skip and did_all_pods_start_within(thresholds_to_skip):
            logger.info(
                f"SKIPPED: All {self.app_context.app} pods recently started within {thresholds_to_skip} seconds"
            )
        else:
            try:
                wait_until(
                    wait_for_pod_initialization_to_start,
                    interval=1,
                    stop_condition=lambda x: x is True,
                    timeout=timeout_sec,
                )
            except TimeoutError:
                raise TimeoutError(
                    f"{self.app_context.app} deployment did not start in {timeout_sec} seconds"
                ) from None
            logger.info(f"{self.app_context.app} deployment started")

    @requires_container
    def wait_for_deployment_to_complete(self, timeout_sec: int = 360, **kwargs):
        """Wait for deployment to complete

        :param timeout_sec: Timeout in seconds
        :param kwargs: Any parameters supported in exec_run()
        """
        logger.info(f"Waiting for deployment of {self.app_context.app} to complete...")
        start_time = time.time()
        cmd = (
            f"kubectl rollout status deployment/{self.app_context.app} -n {self.app_context.namespace} "
            f"--timeout={timeout_sec}s"
        )
        exit_code, output = self.exec_run(cmd, ignore_error=True, **kwargs)
        if exit_code and "timed out waiting for the condition" in output:
            raise TimeoutError(f"Deployment of {self.app_context.app} did not complete:\n{output}")
        self.get_pods()
        logger.info(f"Deployment of {self.app_context.app} completed (took {time.time() - start_time} seconds)")

    @requires_container
    def wait_for_pods_to_become_ready(self, timeout_sec: int = 180, **kwargs):
        """Wait for all pods to become ready

        :param timeout_sec: Timeout in seconds
        :param kwargs: Any parameters supported in exec_run()
        """
        logger.info(f"Waiting for {self.app_context.app} pods to become ready...")
        cmd = f"kubectl wait pods {self._app_filtering_options} --for condition=Ready --timeout={timeout_sec}s"
        exit_code, output = self.exec_run(cmd, ignore_error=True, **kwargs)
        if exit_code and "timed out waiting for the condition" in output:
            raise TimeoutError(f"One or more {self.app_context.app} pods did not become ready:\n{output}")
        logger.info(f"All {self.app_context.app} pods are ready")

    @requires_container
    def _setup_kubeconfig(self):
        """Fetch credentials for the cluster and update kubeconfig"""
        logger.info("Fetching credentilas for the cluster...")
        cmd = f"gcloud container clusters get-credentials {self.cluster_name}"
        if self.region:
            cmd += f" --region {self.region}"
        if self.zone:
            cmd += f" --zone {self.zone}"
        self.exec_run(cmd)

    @requires_container
    def _install_jq(self):
        """Install jq"""
        with Lock("k8s_check_jq"):
            if not K8sConnector.JQ_INSTALLED:
                self.exec_run("apt-get install -y jq")
                K8sConnector.JQ_INSTALLED = True

    @cached_property
    def _app_filtering_options(self) -> str:
        return f"-l {self.app_label_selector_key}={self.app_context.app} -n {self.app_context.namespace}"


class K8sConnectorWithAppContext(K8sConnector):
    """K8s connector with a specific app context"""

    def __init__(self, k8s_container: K8sConnector, app: K8sApp | str):
        try:
            if not isinstance(app, K8sApp):
                app = K8sApp(app)
        except ValueError:
            raise NotImplementedError(
                f"App '{app}' not yet supported. Supported apps: {list(x.value for x in K8sApp._member_map_.values())}"
            )
        super().__init__(
            service_account_file_path=k8s_container.service_account_file_path,
            cluster_name=k8s_container.cluster_name,
            region=k8s_container.region,
            zone=k8s_container.zone,
            run=False,
            timeout=k8s_container.timeout,
        )
        self.container = k8s_container.container
        self._app_context = K8sAppContext(app)
