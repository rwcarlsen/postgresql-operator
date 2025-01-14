#!/usr/bin/env python3
# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

"""Helper class used to manage cluster lifecycle."""
import logging
import os
import pwd
import subprocess
from typing import Set

import requests
from charms.operator_libs_linux.v0.apt import DebianPackage
from charms.operator_libs_linux.v1.systemd import (
    daemon_reload,
    service_running,
    service_start,
)
from jinja2 import Template
from tenacity import (
    RetryError,
    Retrying,
    retry,
    retry_if_result,
    stop_after_attempt,
    stop_after_delay,
    wait_exponential,
    wait_fixed,
)

from constants import USER

logger = logging.getLogger(__name__)

PATRONI_SERVICE = "patroni"


class NotReadyError(Exception):
    """Raised when not all cluster members healthy or finished initial sync."""


class RemoveRaftMemberFailedError(Exception):
    """Raised when a remove raft member failed for some reason."""


class SwitchoverFailedError(Exception):
    """Raised when a switchover failed for some reason."""


class Patroni:
    """This class handles the bootstrap of a PostgreSQL database through Patroni."""

    pass

    def __init__(
        self,
        unit_ip: str,
        storage_path: str,
        cluster_name: str,
        member_name: str,
        planned_units: int,
        peers_ips: Set[str],
        superuser_password: str,
        replication_password: str,
    ):
        """Initialize the Patroni class.

        Args:
            unit_ip: IP address of the current unit
            storage_path: path to the storage mounted on this unit
            cluster_name: name of the cluster
            member_name: name of the member inside the cluster
            peers_ips: IP addresses of the peer units
            planned_units: number of units planned for the cluster
            superuser_password: password for the operator user
            replication_password: password for the user used in the replication
        """
        self.unit_ip = unit_ip
        self.storage_path = storage_path
        self.cluster_name = cluster_name
        self.member_name = member_name
        self.planned_units = planned_units
        self.peers_ips = peers_ips
        self.superuser_password = superuser_password
        self.replication_password = replication_password

    def bootstrap_cluster(self, replica: bool = False) -> bool:
        """Bootstrap a PostgreSQL cluster using Patroni."""
        # Render the configuration files and start the cluster.
        self.configure_patroni_on_unit(replica)
        return self.start_patroni()

    def configure_patroni_on_unit(self, replica: bool = False):
        """Configure Patroni (configuration files and service) on the unit.

        Args:
            replica: whether the unit should be configured as a replica
            (defaults to False, which configures the unit as a leader)
        """
        self._change_owner(self.storage_path)
        self.render_patroni_yml_file(replica)
        self._render_patroni_service_file()
        # Reload systemd services before trying to start Patroni.
        daemon_reload()
        self.render_postgresql_conf_file()

    def _change_owner(self, path: str) -> None:
        """Change the ownership of a file or a directory to the postgres user.

        Args:
            path: path to a file or directory.
        """
        # Get the uid/gid for the postgres user.
        user_database = pwd.getpwnam("postgres")
        # Set the correct ownership for the file or directory.
        os.chown(path, uid=user_database.pw_uid, gid=user_database.pw_gid)

    @property
    @retry(stop=stop_after_attempt(10), wait=wait_exponential(multiplier=1, min=2, max=10))
    def cluster_members(self) -> set:
        """Get the current cluster members."""
        # Request info from cluster endpoint (which returns all members of the cluster).
        cluster_status = requests.get(f"http://{self.unit_ip}:8008/cluster")
        return set([member["name"] for member in cluster_status.json()["members"]])

    def _create_directory(self, path: str, mode: int) -> None:
        """Creates a directory.

        Args:
            path: the path of the directory that should be created.
            mode: access permission mask applied to the
              directory using chmod (e.g. 0o640).
        """
        os.makedirs(path, mode=mode, exist_ok=True)
        # Ensure correct permissions are set on the directory.
        os.chmod(path, mode)
        self._change_owner(path)

    def _get_postgresql_version(self) -> str:
        """Return the PostgreSQL version from the system."""
        package = DebianPackage.from_system("postgresql")
        # Remove the Ubuntu revision from the version.
        return str(package.version).split("+")[0]

    def get_member_ip(self, member_name: str) -> str:
        """Get cluster member IP address.

        Args:
            member_name: cluster member name.

        Returns:
            IP address of the cluster member.
        """
        ip = None
        # Request info from cluster endpoint (which returns all members of the cluster).
        cluster_status = requests.get(f"http://{self.unit_ip}:8008/cluster")
        for member in cluster_status.json()["members"]:
            if member["name"] == member_name:
                ip = member["host"]
                break
        return ip

    def get_primary(self, unit_name_pattern=False) -> str:
        """Get primary instance.

        Args:
            unit_name_pattern: whether to convert pod name to unit name

        Returns:
            primary pod or unit name.
        """
        primary = None
        # Request info from cluster endpoint (which returns all members of the cluster).
        cluster_status = requests.get(f"http://{self.unit_ip}:8008/cluster")
        for member in cluster_status.json()["members"]:
            if member["role"] == "leader":
                primary = member["name"]
                if unit_name_pattern:
                    # Change the last dash to / in order to match unit name pattern.
                    primary = "/".join(primary.rsplit("-", 1))
                break
        return primary

    def are_all_members_ready(self) -> bool:
        """Check if all members are correctly running Patroni and PostgreSQL.

        Returns:
            True if all members are ready False otherwise. Retries over a period of 10 seconds
            3 times to allow server time to start up.
        """
        # Request info from cluster endpoint
        # (which returns all members of the cluster and their states).
        try:
            for attempt in Retrying(stop=stop_after_delay(10), wait=wait_fixed(3)):
                with attempt:
                    cluster_status = requests.get(f"http://{self.unit_ip}:8008/cluster")
        except RetryError:
            return False

        # Check if all members are running and one of them is a leader (primary),
        # because sometimes there may exist (for some period of time) only
        # replicas after a failed switchover.
        return all(
            member["state"] == "running" for member in cluster_status.json()["members"]
        ) and any(member["role"] == "leader" for member in cluster_status.json()["members"])

    @property
    def member_started(self) -> bool:
        """Has the member started Patroni and PostgreSQL.

        Returns:
            True if services is ready False otherwise. Retries over a period of 60 seconds times to
            allow server time to start up.
        """
        try:
            for attempt in Retrying(stop=stop_after_delay(60), wait=wait_fixed(3)):
                with attempt:
                    r = requests.get(f"http://{self.unit_ip}:8008/health")
        except RetryError:
            return False

        return r.json()["state"] == "running"

    def _render_file(self, path: str, content: str, mode: int) -> None:
        """Write a content rendered from a template to a file.

        Args:
            path: the path to the file.
            content: the data to be written to the file.
            mode: access permission mask applied to the
              file using chmod (e.g. 0o640).
        """
        # TODO: keep this method to use it also for generating replication configuration files and
        # move it to an utils / helpers file.
        # Write the content to the file.
        with open(path, "w+") as file:
            file.write(content)
        # Ensure correct permissions are set on the file.
        os.chmod(path, mode)
        self._change_owner(path)

    def _render_patroni_service_file(self) -> None:
        """Render the Patroni configuration file."""
        # Open the template patroni systemd unit file.
        with open("templates/patroni.service.j2", "r") as file:
            template = Template(file.read())
        # Render the template file with the correct values.
        rendered = template.render(conf_path=self.storage_path)
        self._render_file("/etc/systemd/system/patroni.service", rendered, 0o644)

    def render_patroni_yml_file(self, replica: bool = False) -> None:
        """Render the Patroni configuration file."""
        # Open the template patroni.yml file.
        with open("templates/patroni.yml.j2", "r") as file:
            template = Template(file.read())
        # Render the template file with the correct values.
        rendered = template.render(
            conf_path=self.storage_path,
            member_name=self.member_name,
            peers_ips=self.peers_ips,
            scope=self.cluster_name,
            self_ip=self.unit_ip,
            replica=replica,
            superuser=USER,
            superuser_password=self.superuser_password,
            replication_password=self.replication_password,
            version=self._get_postgresql_version(),
        )
        self._render_file(f"{self.storage_path}/patroni.yml", rendered, 0o644)

    def render_postgresql_conf_file(self) -> None:
        """Render the PostgreSQL configuration file."""
        # Open the template postgresql.conf file.
        with open("templates/postgresql.conf.j2", "r") as file:
            template = Template(file.read())
        # Render the template file with the correct values.
        # TODO: add extra configurations here later.
        rendered = template.render(
            listen_addresses="*",
            synchronous_commit="on" if self.planned_units > 1 else "off",
            synchronous_standby_names="*",
        )
        self._create_directory(f"{self.storage_path}/conf.d", mode=0o644)
        self._render_file(f"{self.storage_path}/conf.d/postgresql-operator.conf", rendered, 0o644)

    def start_patroni(self) -> bool:
        """Start Patroni service using systemd.

        Returns:
            Whether the service started successfully.
        """
        service_start(PATRONI_SERVICE)
        return service_running(PATRONI_SERVICE)

    def switchover(self) -> None:
        """Trigger a switchover."""
        # Try to trigger the switchover.
        for attempt in Retrying(stop=stop_after_delay(60), wait=wait_fixed(3)):
            with attempt:
                current_primary = self.get_primary()
                r = requests.post(
                    f"http://{self.unit_ip}:8008/switchover",
                    json={"leader": current_primary},
                )

        # Check whether the switchover was unsuccessful.
        if r.status_code != 200:
            raise SwitchoverFailedError(f"received {r.status_code}")

    @retry(
        retry=retry_if_result(lambda x: not x),
        stop=stop_after_attempt(10),
        wait=wait_exponential(multiplier=1, min=2, max=30),
    )
    def primary_changed(self, old_primary: str) -> bool:
        """Checks whether the primary unit has changed."""
        primary = self.get_primary()
        return primary != old_primary

    def update_cluster_members(self) -> None:
        """Update the list of members of the cluster."""
        # Update the members in the Patroni configuration.
        self.render_patroni_yml_file()

        if service_running(PATRONI_SERVICE):
            self.reload_patroni_configuration()

    def remove_raft_member(self, member_ip: str) -> None:
        """Remove a member from the raft cluster.

        The raft cluster is a different cluster from the Patroni cluster.
        It is responsible for defining which Patroni member can update
        the primary member in the DCS.

        Raises:
            RaftMemberNotFoundError: if the member to be removed
                is not part of the raft cluster.
        """
        # Get the status of the raft cluster.
        raft_status = subprocess.check_output(
            ["syncobj_admin", "-conn", "127.0.0.1:2222", "-status"]
        ).decode("UTF-8")

        # Check whether the member is still part of the raft cluster.
        if not member_ip or member_ip not in raft_status:
            return

        # Remove the member from the raft cluster.
        result = subprocess.check_output(
            ["syncobj_admin", "-conn", "127.0.0.1:2222", "-remove", f"{member_ip}:2222"]
        ).decode("UTF-8")
        if "SUCCESS" not in result:
            raise RemoveRaftMemberFailedError()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def reload_patroni_configuration(self):
        """Reload Patroni configuration after it was changed."""
        requests.post(f"http://{self.unit_ip}:8008/reload")
