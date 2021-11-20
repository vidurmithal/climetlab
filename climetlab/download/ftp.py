# (C) Copyright 2020 ECMWF.
#
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation
# nor does it submit to any jurisdiction.
#


import logging
import os
from ftplib import FTP
from urllib.parse import urlparse

from climetlab.core.settings import SETTINGS

from .downloader import Downloader

LOG = logging.getLogger(__name__)


class FTPDownloader(Downloader):

    supports_parts = False

    def prepare(self, url, download):

        mode = "wb"

        o = urlparse(url)
        assert o.scheme == "ftp"

        if "@" in o.netloc:
            auth, server = o.netloc.split("@")
            user, password = auth.split(":")
        else:
            auth, server = None, o.netloc
            user, password = "anonymous", "anonymous"

        ftp = FTP(
            server,
            timeout=SETTINGS.get("url-download-timeout"),
        )

        if auth:
            ftp.login(user, password)
        else:
            ftp.login()

        ftp.cwd(os.path.dirname(o.path))
        ftp.set_pasv(True)
        self.filename = os.path.basename(o.path)
        self.ftp = ftp

        return ftp.size(self.filename), mode, 0, False

    def transfer(self, f, pbar, watcher):
        total = 0

        def callback(chunk):
            nonlocal total
            watcher()
            f.write(chunk)
            total += len(chunk)
            pbar.update(len(chunk))

        self.ftp.retrbinary(f"RETR {self.filename}", callback)

    def finalise(self):
        self.ftp.close()
