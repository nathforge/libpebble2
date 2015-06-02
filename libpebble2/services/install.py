from __future__ import absolute_import
__author__ = 'katharine'

import uuid

from .blobdb import BlobDBClient, BlobDatabaseID, SyncWrapper, BlobStatus
from .putbytes import PutBytes, PutBytesType
from libpebble2.events import EventSourceMixin
from libpebble2.exceptions import PebbleError
from libpebble2.protocol.apps import AppMetadata, AppRunState, AppRunStateStart, AppFetchRequest, AppFetchResponse, AppFetchStatus
from libpebble2.util.bundle import PebbleBundle

__all__ = ["AppInstaller", "AppInstallError"]


class AppInstallError(PebbleError):
    pass


class AppInstaller(EventSourceMixin):
    def __init__(self, pebble, event_handler, pbw_path, blobdb_client=None):
        self._pebble = pebble
        self._event_handler = event_handler
        self._blobdb = blobdb_client or BlobDBClient(pebble, event_handler)
        EventSourceMixin.__init__(self, self._event_handler)
        self._prepare(pbw_path)

    def _prepare(self, pbw_path):
        self._bundle = PebbleBundle(pbw_path)
        if not self._bundle.is_app_bundle:
            raise AppInstallError("This is not an app bundle.")

        self.total_sent = 0
        self.total_size = self._bundle.zip.getinfo(self._bundle.get_app_path()).file_size
        if self._bundle.has_resources:
            self.total_size += self._bundle.zip.getinfo(self._bundle.get_resource_path()).file_size

        if self._bundle.has_worker:
            self.total_size += self._bundle.zip.getinfo(self._bundle.get_worker_path()).file_size

    def install(self):
        metadata = self._bundle.get_app_metadata()
        app_uuid = metadata['uuid']
        blob_packet = AppMetadata(uuid=app_uuid, flags=metadata['flags'], icon=metadata['icon_resource_id'],
                                  app_version_major=metadata['app_version_major'],
                                  app_version_minor=metadata['app_version_minor'],
                                  sdk_version_major=metadata['sdk_version_major'],
                                  sdk_version_minor=metadata['sdk_version_minor'],
                                  app_face_bg_color=0, app_face_template_id=0, app_name=metadata['app_name'])

        result = SyncWrapper(self._blobdb.insert, BlobDatabaseID.App, app_uuid, blob_packet.serialise()).wait()
        if result != BlobStatus.Success:
            raise AppInstallError("BlobDB error: {!s}".format(result))

        # Start the app.
        self._pebble.send_packet(AppRunState(data=AppRunStateStart(uuid=app_uuid)))

        # Wait for a launch request.
        app_fetch = self._pebble.read_from_endpoint(AppFetchRequest)
        if app_fetch.uuid != app_uuid:
            self._pebble.send_packet(AppFetchResponse(response=AppFetchStatus.InvalidUUID))
            raise AppInstallError("App requested the wrong UUID! Asked for {}; expected {}".format(
                app_fetch.uuid, app_uuid))
        self._broadcast_event('progress', 0, self.total_sent, self.total_size)

        binary = self._bundle.zip.read(self._bundle.get_app_path())
        self._send_part(PutBytesType.Binary, binary, app_fetch.app_id)

        if self._bundle.has_resources:
            resources = self._bundle.zip.read(self._bundle.get_resource_path())
            self._send_part(PutBytesType.Resources, resources, app_fetch.app_id)

        if self._bundle.has_worker:
            worker = self._bundle.zip.read(self._bundle.get_worker_path())
            self._send_part(PutBytesType.Worker, worker, app_fetch.app_id)

    def _send_part(self, type, object, install_id):
        pb = PutBytes(self._pebble, self._event_handler, type, object, app_install_id=install_id)
        pb.register_handler("progress", self._handle_progress)
        pb.send()

    def _handle_progress(self, sent, total_sent, total_length):
        self.total_sent += sent
        self._broadcast_event('progress', sent, self.total_sent, self.total_size)
