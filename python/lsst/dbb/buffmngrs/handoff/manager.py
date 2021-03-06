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
"""DBB handoff manager.
"""

import logging
import os
import queue
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from sqlalchemy import tuple_
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from threading import Thread
from . import Eraser, Finder, Macro, Mover, Porter, Wiper
from .declaratives import Batch, File
from .defaults import Defaults
from .messages import FileMsg
from .utils import get_checksum, get_chunk, setup_db_conn


__all__ = ["Manager"]


logger = logging.getLogger(__name__)


class Manager:
    """The handoff buffer manager.

    The handoff manager transfers file transfers files between a remote
    location, a handoff site, to a designated location at the data facility,
    an endpoint site, and moves successfully transferred files out of the
    buffer to the holding area.

    It uses a SQLite database to persists various information about files it
    it transferred (or failed to do so).

    Parameters
    ----------
    configuration : `dict`
        Configuration of the manager.
    """

    def __init__(self, configuration):
        # Set up database connection.
        config = configuration["database"]
        engine = setup_db_conn(config)
        Session = sessionmaker(bind=engine)
        self.session = Session()

        # Initialize general settings.
        config = configuration.get("general", None)
        settings = asdict(Defaults())
        if config is not None:
            settings.update(config)
        self.num_threads = settings["num_threads"]
        self.pause = settings["pause"]

        # Initialize message queues.
        self.discovered = queue.Queue()
        self.pending = queue.Queue()
        self.processed = queue.Queue()
        self.completed = queue.Queue()

        self.transfers = queue.Queue()

        # Define tasks related to managing the buffer.
        handoff = configuration["handoff"]
        self.finder = Finder(handoff, self.discovered)
        mover = Mover(handoff, self.processed, self.completed)
        eraser = Eraser(handoff, exp_time=settings["expiration_time"])
        self.cleaner = Macro()
        self.cleaner.add(mover)
        self.cleaner.add(eraser)

        # Define tasks related to file transfer.
        endpoint = configuration["endpoint"]
        self.porter = Porter(endpoint, self.pending, self.transfers,
                             chunk_size=settings["chunk_size"],
                             timeout=settings["timeout"])
        self.wiper = Wiper(endpoint)

    def run(self):
        """Start the manager.
        """
        logger.info("Starting monitoring the buffer...")
        while True:
            # Scan source location for files.
            #
            # Note
            # ----
            # Produces file items and enqueues them in the discovery queue.
            logger.info("Scanning buffer for new files.")
            start = time.time()
            self.finder.run()
            end = time.time()
            duration = end - start
            logger.info(f"Scan completed in {duration:.2f} sec., "
                        f"{self.discovered.qsize()} file(s) found.")

            # Go to slumber for a given time interval before starting next
            # scan, if no files were found.
            if self.discovered.qsize() == 0:
                logger.info(f"Next scan in {self.pause} sec.")
                time.sleep(self.pause)
                continue

            # Create database entries for the files in the buffer.
            #
            # Note
            # ----
            # Consumes file items from the discovery queue and uses them
            # to populate the pending queue.
            self._add_files(self.discovered, self.pending)

            # Copy files to the remote location.
            #
            # Note
            # ----
            # Consumes file items from the pending queue and produces
            # transfer items which it uses to populate the transfer queue.
            # The transfer queue contains both successful and failed
            # transfer attempts.
            logger.info(f"Transferring files.")
            start = time.time()
            threads = []
            for _ in range(self.num_threads):
                t = Thread(target=self.porter.run)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
            self.wiper.run()
            end = time.time()
            duration = end - start
            logger.info(f"Transfer attempts completed in {duration:.2f} sec.")

            # Create database entries for the transfers made.
            #
            # Note
            # ----
            # Consumes transfer items from the transfer queue and populates
            # the processed queue with file items.
            self._add_transfers(self.transfers, self.processed)

            # Move successfully transferred files to the holding area.
            #
            # Note
            # ----
            # Consumes files items from processed queue and populates the
            # completed queue with file items.
            self.cleaner.run()

            # Updates held time.
            #
            # Note
            # ----
            # Consumes file items from the completed queue.
            self._update_files(self.completed)

            # Go to slumber for a given time interval.
            logger.info(f"Next scan in {self.pause} sec.")
            time.sleep(self.pause)

    def _add_files(self, inp, out, chunk_size=10):
        """Create database entries for files found in the buffer.

        Parameters
        ----------
        inp : queue.Queue
            Input queue with file items that need to be added to the database.
        out : queue.Queue
            Output queue for file items which were succesfully added to the
            database.
        chunk_size : `int`, optional
            Number of items to grab from the queue, defaults to 10.
        """
        while not inp.empty():
            items = get_chunk(inp, size=chunk_size)

            files = []
            for item in items:
                path = os.path.join(item.head, item.tail, item.name)
                cksm = get_checksum(path)

                # Skip already existing database entries.
                file_ = self.session.query(File).\
                    filter(File.relpath == item.tail,
                           File.filename == item.name,
                           File.checksum == cksm).first()
                if file_ is not None:
                    continue

                file_ = File(
                    relpath=item.tail,
                    filename=item.name,
                    checksum=cksm,
                    size_bytes=item.size,
                    created_on=datetime.fromtimestamp(item.timestamp)
                )
                files.append(file_)

            # Try to commit changes to the database.  If the commit was
            # successful, populate the output queue with files that need to
            # be transferred.
            self.session.add_all(files)
            try:
                self.session.commit()
            except (DBAPIError, SQLAlchemyError) as ex:
                msg = f"adding new files failed: {ex}"
                logger.error(msg)
            else:
                for item in items:
                    out.put(item)

    def _add_transfers(self, transfers, files, chunk_size=10):
        """Create database entries for completed transfer batches.

        Parameters
        ----------
        transfers : queue.Queue
            Input queue with transfer items.
        files : queue.Queue
            Output queue for file items.
        chunk_size : `int`, optional
            Number of items to grab from the queue, defaults to 10.
        """
        while not transfers.empty():
            items = get_chunk(transfers, size=chunk_size)

            batches = []
            transferred = []
            for item in items:
                records = []
                try:
                    records = self.session.query(File). \
                        filter(tuple_(File.relpath, File.filename).
                               in_([(p, n) for _, p, n in item.files])).all()
                except (DBAPIError, SQLAlchemyError) as ex:
                    msg = f"retrieving database records of files in a batch " \
                          f"failed: {ex}"
                    logger.error(msg)
                if not records:
                    continue

                # Create an entry for a given transfer batch.
                batch = Batch(
                    pre_start_time=datetime.fromtimestamp(item.pre_start),
                    pre_duration=timedelta(seconds=item.pre_duration),
                    trans_start_time=datetime.fromtimestamp(item.pre_start),
                    trans_duration=timedelta(seconds=item.pre_duration),
                    post_start_time=datetime.fromtimestamp(item.pre_start),
                    post_duration=timedelta(seconds=item.pre_duration),
                    size_bytes=item.size,
                    rate_mbytes_per_sec=item.rate,
                    status=item.status,
                    err_msg=item.error
                )
                batch.files.extend(records)
                batches.append(batch)

                # Create messages corresponding to successfully transferred
                # files.
                if item.status == 0:
                    for head, tail, name in item.files:
                        item = FileMsg(head=head, tail=tail, name=name)
                        transferred.append(item)

            # Try to commit changes to the database.  If the commit was
            # successful, populate the output queue with files that were
            # successfully transferred.
            self.session.add_all(batches)
            try:
                self.session.commit()
            except (DBAPIError, SQLAlchemyError) as ex:
                msg = f"adding new transfer batches failed: {ex}"
                logger.error(msg)
            else:
                for item in transferred:
                    files.put(item)

    def _update_files(self, inp, chunk_size=10):
        """Add move time to file database entries.

        Parameters
        ----------
        inp : queue.Queue
            Input queue with file items.
        chunk_size : `int`, optional
            Number of items to grab from the queue, defaults to 10.
        """
        while not inp.empty():
            items = get_chunk(inp, size=chunk_size)
            for item in items:
                tail, name = item.tail, item.name
                rec = self.session.query(File).\
                    filter(File.relpath == tail, File.filename == name).\
                    first()
                if rec is not None:
                    rec.held_on = datetime.fromtimestamp(item.timestamp)
            try:
                self.session.commit()
            except (DBAPIError, SQLAlchemyError) as ex:
                msg = f"updating files' held times failed: {ex}"
                logger.error(msg)
