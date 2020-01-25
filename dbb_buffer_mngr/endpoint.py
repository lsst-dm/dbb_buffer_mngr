import collections
import errno
import logging
import os
import queue
import shlex
import subprocess
from .command import Command


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
        command will be terminated. Defaults to None which means

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

        self.dest = (config["user"], config["host"], config["buffer"])
        self.temp = config["staging"]
        self.size = chunk_size
        self.time = timeout

        self.todo = awaiting
        self.done = completed

    def run(self):
        """Transfer files to the endpoint site.
        """
        user, host, root = self.dest
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
                dst = os.path.join(root, tail)
                tmp = os.path.join(self.temp, tail)

                # Create the directories at the remote location.
                cmd = f"ssh {user}@{host} mkdir -p {dst} {tmp}"
                status, stdout, stderr = execute(cmd, timeout=self.time)
                if status != 0:
                    msg = f"Command '{cmd}' failed with error: '{stderr}'"
                    logger.warning(msg)
                    continue

                # Transfer files to the remote staging area.
                sources = [os.path.join(src, fn) for fn in files]
                cmd = f"scp -BCpq {' '.join(sources)} {user}@{host}:{tmp}"
                status, stdout, stderr = execute(cmd, timeout=self.time)
                if status != 0:
                    msg = f"Command '{cmd}' failed with error: '{stderr}'"
                    logger.warning(msg)
                    continue

                # Move successfully transferred files to the final location.
                for fn in files:
                    s = os.path.join(tmp, fn)
                    d = os.path.join(dst, fn)
                    cmd = f"ssh {user}@{host} mv {s} {d}"
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
        self.dest = (config["user"], config["host"], config["staging"])
        self.time = timeout

    def run(self):
        """Remove empty directories from the staging area.
        """
        user, host, path = self.dest
        cmd = f"ssh {user}@{host} find {path} -type d -empty -mindepth 1 -delete"
        status, stdout, stderr = execute(cmd, timeout=self.time)
        if status != 0:
            msg = f"Command '{cmd}' failed with error: '{stderr}'"
            logger.warning(msg)


def execute(cmd, timeout=None):
    """Run a shell command.

    Parameters
    ----------
    cmd : str
        String representing the command, its options and arguments.
    timeout : int, optional
        Time (in seconds) after the child process will be killed.

    Returns
    -------
    int
        Shell command exit status, 0 if successful, non-zero otherwise.
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