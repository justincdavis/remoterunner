# Copyright (c) 2024 Justin Davis (davisjustin302@gmail.com)
#
# MIT License
# ruff: noqa: S507
from __future__ import annotations

import contextlib
import json
import logging
import socket
import time
from functools import partial
from typing import TYPE_CHECKING

import paramiko
import scp  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


_log = logging.getLogger(__name__)


def check_bash(client: paramiko.SSHClient) -> str | None:
    """
    Check if bash is available on the remote machine.

    Parameters
    ----------
    client : paramiko.SSHClient
        The client to check for bash on.

    Returns
    -------
    str | None
        The version of bash found.

    """

    def _check_bash(client: paramiko.SSHClient, bash: str) -> str | None:
        with contextlib.suppress(paramiko.SSHException):
            _, bash_stdout, bash_stderr = client.exec_command(f"{bash} --version")
            if not bash_stderr.read().decode() and len(bash_stdout.read().decode()) > 0:
                return bash
        return None

    # checking the basic versions of bash
    basic_bash = ["bash", "/bin/bash", "/usr/bin/bash"]
    for bash in basic_bash:
        bash_version = _check_bash(client, bash)
        if bash_version is not None:
            return bash_version

    # check if the locate command is available
    try:
        _, locate_stdout, locate_stderr = client.exec_command("locate bash")
        if locate_stderr.read().decode():
            return None
        locate_output = locate_stdout.read().decode().split("\n")
        potential_bash = []
        for pbash in locate_output:
            pbash_name = pbash.split("/")[-1]
            # bash binary wont have suffix
            # if "." in pbash_name:
            #     continue
            if pbash_name == "bash":
                potential_bash.append(pbash)
    except paramiko.SSHException:
        return None

    # check the potential bash binaries
    for pbash in potential_bash:
        bash_version = _check_bash(client, pbash)
        if bash_version is not None:
            return bash_version

    return None


def run_script(
    machine_name: str,
    hostname: str,
    user: str,
    password: str,
    port: int | None,
    script_path: Path,
    output_dir_path: Path,
    deps: Path,
    datafiles: list[Path] | None = None,
    dep_scripts: list[Path] | None = None,
    dep_dirs: list[Path] | None = None,
    timeout: int = 5,
    *,
    transfer_run_dir: bool | None = None,
) -> bool:
    """
    Run the script on the remote machine.

    Parameters
    ----------
    machine_name : str
        The name of the machine.
    hostname : str
        The hostname of the machine.
    user : str
        The user to connect as.
    password : str
        The password to connect with.
    port : int
        The port to connect on.
    script_path : Path
        The path to the script to run.
    output_dir_path : Path
        The path to the output directory.
    datafiles : list[Path]
        The data files to transfer.
    deps : Path | None
        The dependencies to install.
    dep_scripts : list[Path] | None
        The dependency scripts to run.
    dep_dirs : list[Path] | None
        The dependency directories to transfer.
    timeout : int
        The timeout for the connection.
    transfer_run_dir : bool | None
        Whether to transfer the run directory back to the host.
        If None, the default is False.

    Returns
    -------
    bool
        True if the script ran successfully, False otherwise.

    """

    def write_stdout_stderr(output_dir: Path, stdout: str, stderr: str) -> None:
        """Write the stdout and stderr to files before exit."""
        _log.debug(f"{machine_name}: Exiting")
        # write stdout, stderr to files
        stdout_path = output_dir / "stdout.txt"
        stderr_path = output_dir / "stderr.txt"
        stdout_path.touch(exist_ok=True)
        stderr_path.touch(exist_ok=True)
        stdout_path.write_text(stdout)
        stderr_path.write_text(stderr)
        _log.debug(f"{machine_name}: Wrote stdout, stderr to files")

    def write_output_json(output_dir: Path, st: int, et: int, total: int) -> None:
        """Write the output.json file."""
        output_json = {
            "start_time": st,
            "end_time": et,
            "total_time": total,
        }
        output_json_path = output_dir / "output.json"
        output_json_path.touch(exist_ok=True)
        with output_json_path.open("w") as f:
            json.dump(output_json, f, indent=4)

    def wrap_command(bash: str, command: str) -> str:
        """Wrap the command in the bash command."""
        return f"{bash} -c '{command}'"

    # begin run_script
    _log.debug(f"{machine_name}: Starting run_script")

    # handle and argument defaults
    if transfer_run_dir is None:
        transfer_run_dir = False

    # begin running logs for stdout and stderr of the script
    stdout = ""
    stderr = ""

    # create client and connect
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=hostname,
            username=user,
            password=password,
            port=port if port is not None else 22,
            timeout=timeout,
        )
    except socket.timeout:
        _log.error(f"{machine_name}: Connection timed out, exiting.")
        write_stdout_stderr(output_dir_path, stdout, stderr)
        return False
    except OSError as er:
        _log.error(f"{machine_name}: Socket error: {er}")
        write_stdout_stderr(output_dir_path, stdout, stderr)
        return False
    _log.debug(f"{machine_name}: Connected")

    # check for python3
    try:
        _, py3_stdout, py3_stderr = client.exec_command("python3 --version")
        _log.debug(f"{machine_name}: Python3 found")
        stdout += py3_stdout.read().decode()
        stderr += py3_stderr.read().decode()
    except paramiko.SSHException:
        _log.error(f"{machine_name}: Python3 not found, exiting.")
        write_stdout_stderr(output_dir_path, stdout, stderr)
        return False

    # check for bash and create command wrapper
    bash = check_bash(client)
    com_wrap: Callable[[str], str] = partial(wrap_command, bash)
    if bash is None:
        _log.error(f"{machine_name}: Bash not found, exiting.")
        write_stdout_stderr(output_dir_path, stdout, stderr)
        return False
    _log.debug(f"{machine_name}: Bash found")

    # create new directory for which to run the script and create
    # the virtual environment
    base_directory = "runs"
    run_directory = f"run_{int(time.time())}"
    machine_directory = f"{base_directory}/{run_directory}"
    try:
        mk_command = f"mkdir -p {machine_directory}"
        _, mk_stdoutt, mk_stderr = client.exec_command(com_wrap(mk_command))
        _log.debug(
            f"{machine_name}: Created directory for execution, {machine_directory}",
        )
        stdout += mk_stdoutt.read().decode()
        stderr += mk_stderr.read().decode()
    except paramiko.SSHException:
        _log.error(f"{machine_name}: Could not create virtualenv directory")
        write_stdout_stderr(output_dir_path, stdout, stderr)
        return False

    # create the scp_client
    try:
        scp_client = scp.SCPClient(client.get_transport())
        _log.debug(f"{machine_name}: Created SCPClient")
    except scp.SCPException as err:
        _log.error(f"{machine_name}: Could not create SCPClient: {err}")
        write_stdout_stderr(output_dir_path, stdout, stderr)
        return False

    # transfer the files
    try:
        scp_client.put(str(script_path), f"{machine_directory}/script.py")
        _log.debug(
            f"{machine_name}: Transferred {script_path} to {machine_directory}/script.py",
        )
        if datafiles is not None:
            for datafile in datafiles:
                scp_client.put(str(datafile), f"{machine_directory}/{datafile.name}")
                _log.debug(
                    f"{machine_name}: Transferred datafile {datafile} to {machine_directory}/{datafile.name}",
                )
        if deps is not None:
            scp_client.put(str(deps), f"{machine_directory}/requirements.txt")
            _log.debug(f"{machine_name}: Transferred requirements file")
        if dep_scripts is not None:
            for dep_script in dep_scripts:
                scp_client.put(
                    str(dep_script),
                    f"{machine_directory}/{dep_script.name}",
                )
                _log.debug(
                    f"{machine_name}: Transferred dependency script {dep_script}",
                )
        if dep_dirs is not None:
            for dep_dir in dep_dirs:
                scp_client.put(
                    str(dep_dir),
                    f"{machine_directory}/{dep_dir.name}",
                    recursive=True,
                )
                _log.debug(
                    f"{machine_name}: Transferred dependency directory {dep_dir}",
                )
        _log.debug(f"{machine_name}: Transferred all files")
    except scp.SCPException as err:
        _log.error(f"{machine_name}: Could not transfer files: {err}")
        write_stdout_stderr(output_dir_path, stdout, stderr)
        return False

    # ensure virtualenv is installed
    try:
        upip_com = "python3 -m pip install --upgrade pip"
        # assume the upgrade pip line will be successfull
        client.exec_command(com_wrap(upip_com))
        venv_install_com = "python3 -m pip install virtualenv"
        _, venv_install_stdout, venv_install_stderr = client.exec_command(
            com_wrap(venv_install_com),
        )
        stdout += venv_install_stdout.read().decode()
        stderr += venv_install_stderr.read().decode()
    except paramiko.SSHException:
        _log.error(f"{machine_name}: Could not install virtualenv")
        write_stdout_stderr(output_dir_path, stdout, stderr)
        return False

    # create the virtual environment
    try:
        venv_create_com = f"python3 -m venv {machine_directory}/env"
        _, venv_create_stdout, venv_create_stderr = client.exec_command(
            com_wrap(venv_create_com),
        )
        stdout += venv_create_stdout.read().decode()
        stderr += venv_create_stderr.read().decode()
        if venv_create_stderr.read().decode():
            _log.error(f"{machine_name}: Could not create virtualenv")
            write_stdout_stderr(output_dir_path, stdout, stderr)
            return False
    except paramiko.SSHException:
        _log.error(f"{machine_name}: Could not create virtualenv")
        write_stdout_stderr(output_dir_path, stdout, stderr)
        return False

    # install the dependencies
    try:
        install_dep_com = f"source {machine_directory}/env/bin/activate;"
        install_dep_com += (
            f"python3 -m pip install -r {machine_directory}/requirements.txt;"
        )
        install_dep_com += "deactivate"
        _, install_dep_stdout, install_dep_stderr = client.exec_command(
            com_wrap(install_dep_com),
        )
        env_stdout_text = install_dep_stdout.read().decode()
        env_stderr_text = install_dep_stderr.read().decode()
        stdout += env_stdout_text
        stderr += env_stderr_text

        # evaluate the output of the virtualenv installation
        for line in env_stderr_text.split("\n"):
            line2 = line.strip()
            if not line2:
                continue
            # stderr contains text that is not upgrade notice
            if "[notice]" not in line2:
                _log.error(
                    f"{machine_name}: Error installing the dependencies",
                )
                write_stdout_stderr(output_dir_path, stdout, stderr)
                return False
    except paramiko.SSHException:
        _log.error(f"{machine_name}: Could not install dependencies")
        write_stdout_stderr(output_dir_path, stdout, stderr)
        return False

    # run the script
    try:
        # start time
        start_time = int(time.time())

        # actual script run
        command = f"cd {machine_directory};"
        command += "source env/bin/activate;"
        command += "python3 script.py;"
        command += "deactivate"
        _, script_stdout, script_stderr = client.exec_command(com_wrap(command))
        stdout += script_stdout.read().decode()
        stderr += script_stderr.read().decode()

        # end time
        end_time = int(time.time())
        total_time = end_time - start_time
        _log.debug(f"{machine_name}: Script ran in {total_time} seconds")
    except paramiko.SSHException:
        _log.error(f"{machine_name}: Could not run script")
        write_stdout_stderr(output_dir_path, stdout, stderr)
        return False

    # clean the environment
    try:
        clean_env_com = f"rm -rf {machine_directory}/env"
        client.exec_command(com_wrap(clean_env_com))
        _log.debug(f"{machine_name}: Cleaned up environment")
    except paramiko.SSHException:
        _log.warning(f"{machine_name}: Could not clean up environment")

    # transfer the run directory into the output directory for the machine
    if transfer_run_dir:
        try:
            scp_client.get(
                f"{machine_directory}",
                f"{output_dir_path}/{run_directory}",
                recursive=True,
            )
            _log.debug(f"{machine_name}: Transferred output directory")
        except scp.SCPException as err:
            _log.error(
                f"{machine_name}: Could not transfer output directory back to host: {err}",
            )
            write_stdout_stderr(output_dir_path, stdout, stderr)
            return False

    # write final output files
    write_output_json(output_dir_path, start_time, end_time, total_time)
    write_stdout_stderr(output_dir_path, stdout, stderr)

    return True