# This file is part of dbb_buffer_mngr.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import collections
import errno
import logging
import os
import queue
import shlex
import subprocess
from .abcs import Command

__all__ = ['Porter', 'Wiper']


logger = logging.getLogger(__name__)


Location = collections.namedtuple("Location", ["head", "tail"])


class Porter(Command):
    """Command transferring files between handoff and endpoint sites.

    To make file transfers look like atomic operations, files are not placed
    in directly in the buffer, but are initially transferred to a separate
    location on the endpoint site, a staging area.  Once the transfer of a
    file is finished, it is moved to the buffer.

    Parameters
    ----------
    config : dict
        Configuration of the endpoint where files should be transferred to.
    awaiting : queue.Queue
        Files that need to be transferred.
    completed : queue.Queue
        Files that were transferred successfully.
    chunk_size : int, optional
        Number of files to process in a single iteration of the transfer
        loop, defaults to 1.
    timeout : int, optional
        Time (in seconds) after which the child process executing a bash
        command will be terminated. If None (default), the command will wait
        indefinitely for the child process to complete.

    Raises
    ------
    ValueError
        If endpoint's specification is invalid.
    """

    def __init__(self, config, awaiting, completed, chunk_size=1, timeout=None):
        required = {"user", "host", "buffer", "staging"}
        missing = required - set(config)
        if missing:
            msg = f"Invalid configuration: {', '.join(missing)} not provided."
            logger.critical(msg)
            raise ValueError(msg)

        port = config.get("port", 22)
        self.stage = (config["user"], config["host"], port, config["staging"])
        self.buffer = config["buffer"]
        self.size = chunk_size
        self.time = timeout

        self.scp_opts = [
            "-B",         # don't ask for passwords or passphrases
            "-C",         # enable compression
            "-p",         # preserve modification and access times
            "-q",         # disable progress meter, diagnostic messages
            f"-P {port}"  # set the port to connect to on the remote host
        ]

        self.ssh_opts = []
        ssh_config = config.get("ssh", None)
        if ssh_config is not None:
            self.ssh_opts = [f"-o {k}={v}" for k, v in ssh_config.items()]

        self.todo = awaiting
        self.done = completed

    def run(self):
        """Transfer files to the endpoint site.
        """
        user, host, port, root = self.stage
        scp_opts = " ".join(self.scp_opts)
        ssh_opts = " ".join(self.ssh_opts)
        while not self.todo.empty():
            chunk = []
            for _ in range(self.size):
                try:
                    topdir, subdir, file = self.todo.get(block=False)
                except queue.Empty:
                    break
                else:
                    chunk.append((topdir, subdir, file))
            if not chunk:
                continue

            mapping = dict()
            for topdir, subdir, file in chunk:
                loc = Location(topdir, subdir)
                mapping.setdefault(loc, []).append(file)
            for loc, files in mapping.items():
                head, tail = loc
                src = os.path.join(head, tail)
                stage = os.path.join(root, tail)
                buffer = os.path.join(self.buffer, tail)

                # Create necessary subdirectory in the staging area on the
                # endpoint site.
                cmd = f"ssh -p {port} {ssh_opts} {user}@{host} " \
                      f"mkdir -p {stage}"
                status, stdout, stderr = execute(cmd, timeout=self.time)
                if status != 0:
                    msg = f"Command '{cmd}' failed with error: '{stderr}'"
                    logger.warning(msg)
                    continue

                # Transfer files to the staging area.
                sources = [os.path.join(src, fn) for fn in files]
                cmd = f"scp {scp_opts} {ssh_opts} " \
                      f"{' '.join(sources)} {user}@{host}:{stage}"
                status, stdout, stderr = execute(cmd, timeout=self.time)
                if status != 0:
                    msg = f"Command '{cmd}' failed with error: '{stderr}'"
                    logger.warning(msg)
                    continue

                # Create necessary subdirectory in the the buffer on
                # the endpoint site.
                cmd = f"ssh -p {port} {ssh_opts} {user}@{host} " \
                      f"mkdir -p {buffer}"
                status, stdout, stderr = execute(cmd, timeout=self.time)
                if status != 0:
                    msg = f"Command '{cmd}' failed with error: '{stderr}'"
                    logger.warning(msg)
                    continue

                # Move successfully transferred files to the endpoint's buffer.
                for fn in files:
                    source = os.path.join(stage, fn)
                    target = os.path.join(buffer, fn)
                    cmd = f"ssh -p {port} {user}@{host} mv {source} {target}"
                    status, stdout, stderr = execute(cmd, timeout=self.time)
                    if status != 0:
                        msg = f"Command '{cmd}' failed with error: '{stderr}'"
                        logger.warning(msg)
                        continue
                    self.done.put((head, tail, fn))


class Wiper(Command):
    """Command removing empty directories from the staging area.

    Parameters
    ----------
    config : dict
        Configuration of the endpoint where empty directories should be
        removed.
    timeout : int, optional
        Time (in seconds) after which the child process executing a bash
        command will be terminated. If None (default), the command will wait
        indefinitely for the child process to complete.

    Raises
    ------
    ValueError
        If endpoint's specification is invalid.
    """

    def __init__(self, config, timeout=None):
        required = {"user", "host", "staging"}
        missing = required - set(config)
        if missing:
            msg = f"Invalid configuration: {', '.join(missing)} not provided."
            logger.critical(msg)
            raise ValueError(msg)

        port = config.get("port", 22)
        self.dest = (config["user"], config["host"], port, config["staging"])
        self.time = timeout

    def run(self):
        """Remove empty directories from the staging area.
        """
        user, host, port, path = self.dest
        cmd = f"ssh -p {port} {user}@{host} " \
              f"find {path} -type d -empty -mindepth 1 -delete"
        status, stdout, stderr = execute(cmd, timeout=self.time)
        if status != 0:
            msg = f"Command '{cmd}' failed with error: '{stderr}'"
            logger.warning(msg)


def execute(cmd, timeout=None):
    """Run a shell command.

    Parameters
    ----------
    cmd : basestring
        String representing the command, its options and arguments.
    timeout : int, optional
        Time (in seconds) after which the child process executing a bash
        command will be terminated. If None (default), the command will wait
        indefinitely for the child process to complete.

    Returns
    -------
    (int, basestring, basestring)
        Shell command exit status, stdout, and stderr
    """
    logger.debug(f"Executing {cmd}.")

    args = shlex.split(cmd)
    opts = dict(capture_output=True, timeout=timeout, check=True, text=True)
    try:
        proc = subprocess.run(args, **opts)
    except subprocess.CalledProcessError as ex:
        status = errno.EREMOTEIO
        stdout, stderr = ex.stdout, ex.stderr
    except subprocess.TimeoutExpired as ex:
        status = errno.ETIME
        stdout, stderr = ex.stdout, ex.stderr
    else:
        status = proc.returncode
        stdout, stderr = proc.stdout, proc.stderr

    msg = f"(status: {status}, output: '{stdout}', errors: '{stderr}')."
    logger.debug("Finished " + msg)
    return status, stdout, stderr
