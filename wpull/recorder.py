import abc
import base64
import contextlib
import gettext
import gzip
import hashlib
import io
import sys
import tempfile
import time
import uuid

from wpull.namevalue import NameValueRecord
import wpull.util


_ = gettext.gettext


class BaseRecorder(object, metaclass=abc.ABCMeta):
    @abc.abstractmethod
    @contextlib.contextmanager
    def session(self):
        pass


class BaseRecorderSession(object, metaclass=abc.ABCMeta):
    def request(self, request):
        pass

    def request_data(self, data):
        pass

    def pre_response(self, response):
        pass

    def response(self, response):
        pass

    def response_data(self, data):
        pass


class DemuxRecorder(BaseRecorder):
    def __init__(self, recorders):
        super().__init__()
        self._recorders = recorders

    @contextlib.contextmanager
    def session(self):
        dmux = DemuxRecorderSession(self._recorders)
        with dmux:
            yield dmux


class DemuxRecorderSession(BaseRecorderSession):
    def __init__(self, recorders):
        super().__init__()
        self._recorders = recorders
        self._sessions = None
        self._contexts = None

    def __enter__(self):
        self._contexts = [recorder.session() for recorder in self._recorders]
        self._sessions = [context.__enter__() for context in self._contexts]

    def request(self, request):
        for session in self._sessions:
            session.request(request)

    def request_data(self, data):
        for session in self._sessions:
            session.request_data(data)

    def pre_response(self, response):
        for session in self._sessions:
            session.pre_response(response)

    def response(self, response):
        for session in self._sessions:
            session.response(response)

    def response_data(self, data):
        for session in self._sessions:
            session.response_data(data)

    def __exit__(self, *args):
        for context in self._contexts:
            context.__exit__(*args)


class WARCRecord(object):
    VERSION = 'WARC/1.0'
    WARC_TYPE = 'WARC-Type'
    CONTENT_TYPE = 'Content-Type'
    WARC_DATE = 'WARC-Date'
    WARC_RECORD_ID = 'WARC-Record-ID'
    WARCINFO = 'warcinfo'
    WARC_FIELDS = 'application/warc-fields'
    REQUEST = 'request'
    RESPONSE = 'response'
    TYPE_REQUEST = 'application/http;msgtype=request'
    TYPE_RESPONSE = 'application/http;msgtype=response'

    def __init__(self):
        self.fields = NameValueRecord()
        self.block_file = None

    def set_common_fields(self, warc_type, content_type):
        self.fields[self.WARC_TYPE] = warc_type
        self.fields[self.CONTENT_TYPE] = content_type
        self.fields[self.WARC_DATE] = wpull.util.datetime_str()
        self.fields[self.WARC_RECORD_ID] = '<{0}>'.format(uuid.uuid4().urn)

    def compute_checksum(self, payload_offset=None):
        if not self.block_file:
            self.fields['Content-Length'] = '0'
            return

        block_hasher = hashlib.sha1()
        payload_hasher = hashlib.sha1()

        with wpull.util.reset_file_offset(self.block_file):
            if payload_offset is not None:
                data = self.block_file.read(payload_offset)
                block_hasher.update(data)

            while True:
                data = self.block_file.read(4096)
                if data == b'':
                    break
                block_hasher.update(data)
                payload_hasher.update(data)

            content_length = self.block_file.tell()

        content_hash = block_hasher.digest()

        self.fields['WARC-Block-Digest'] = 'sha1:{0}'.format(
            base64.b32encode(content_hash).decode()
        )

        if payload_offset is not None:
            payload_hash = payload_hasher.digest()
            self.fields['WARC-Payload-Digest'] = 'sha1:{0}'.format(
                base64.b32encode(payload_hash).decode()
            )

        self.fields['Content-Length'] = str(content_length)

    def __iter__(self):
        yield self.VERSION.encode()
        yield b'\r\n'
        yield bytes(self.fields)
        yield b'\r\n'

        with wpull.util.reset_file_offset(self.block_file):
            while True:
                data = self.block_file.read(4096)
                if data == b'':
                    break
                yield data

        yield b'\r\n\r\n'

    def __str__(self):
        return ''.join(iter(self))


class WARCRecorder(BaseRecorder):
    def __init__(self, filename, compress=True, extra_fields=None):
        self._filename = filename
        self._gzip_enabled = compress
        self._warcinfo_record = WARCRecord()

        self._populate_warcinfo(extra_fields)
        self.write_record(self._warcinfo_record)

    def _populate_warcinfo(self, extra_fields=None):
        self._warcinfo_record.set_common_fields(
            WARCRecord.WARCINFO, WARCRecord.WARC_FIELDS)

        info_fields = NameValueRecord()
        info_fields['Software'] = 'Wpull/{0}'.format(wpull.version.__version__)
        info_fields['format'] = 'WARC File Format 1.0'
        info_fields['conformsTo'] = \
            'http://bibnum.bnf.fr/WARC/WARC_ISO_28500_version1_latestdraft.pdf'

        if extra_fields:
            for name, value in extra_fields:
                info_fields.add(name, value)

        self._warcinfo_record.block_file = io.BytesIO(
            bytes(info_fields) + b'\r\n')
        self._warcinfo_record.compute_checksum()

    @contextlib.contextmanager
    def session(self):
        recorder_session = WARCRecorderSession(self)
        yield recorder_session

    def write_record(self, record):
        # TODO: probably not a good idea to modifiy arguments passed to us
        record.fields['WARC-Warcinfo-ID'] = self._warcinfo_record.fields[
            WARCRecord.WARC_RECORD_ID]

        if self._gzip_enabled:
            open_func = gzip.GzipFile
        else:
            open_func = open

        with open_func(self._filename, mode='ab') as out_file:
            for data in record:
                out_file.write(data)


class WARCRecorderSession(BaseRecorderSession):
    def __init__(self, recorder):
        self._recorder = recorder
        self._request = None
        self._request_record = None

    def request(self, request):
        self._request = request
        self._request_record = record = WARCRecord()
        record.set_common_fields(WARCRecord.REQUEST, WARCRecord.TYPE_REQUEST)
        record.fields['WARC-Target-URI'] = request.url_info.url
        record.fields['WARC-IP-Address'] = request.address[0]
        record.block_file = tempfile.SpooledTemporaryFile(max_size=1048576)

        with wpull.util.reset_file_offset(record.block_file):
            for data in request:
                record.block_file.write(data)

        payload_offset = len(b''.join(request.iter_header()))
        record.compute_checksum(payload_offset=payload_offset)

        self._recorder.write_record(record)

    def response(self, response):
        record = WARCRecord()
        record.set_common_fields(WARCRecord.RESPONSE, WARCRecord.TYPE_RESPONSE)
        record.fields['WARC-Target-URI'] = self._request.url_info.url
        record.fields['WARC-IP-Address'] = self._request.address[0]
        record.fields['WARC-Concurrent-To'] = self._request_record.fields[
            WARCRecord.WARC_RECORD_ID]
        record.block_file = tempfile.SpooledTemporaryFile(max_size=1048576)

        with wpull.util.reset_file_offset(record.block_file):
            for data in response:
                record.block_file.write(data)

        payload_offset = len(b''.join(response.iter_header()))
        record.compute_checksum(payload_offset=payload_offset)

        self._recorder.write_record(record)


class DebugPrintRecorder(BaseRecorder):
    @contextlib.contextmanager
    def session(self):
        print('Session started')
        try:
            yield DebugPrintRecorderSession()
        finally:
            print('Session ended')


class DebugPrintRecorderSession(BaseRecorderSession):
    def request(self, request):
        print(request)

    def request_data(self, data):
        print(data)

    def pre_response(self, response):
        print(response)

    def response(self, response):
        print(response)

    def response_data(self, data):
        print(data)


class PrintServerResponseRecorder(BaseRecorder):
    @contextlib.contextmanager
    def session(self):
        yield PrintServerResponseRecorderSession()


class PrintServerResponseRecorderSession(BaseRecorderSession):
    def response(self, response):
        print(''.join(data.decode() for data in response.iter_header()))


class ProgressRecorder(BaseRecorder):
    def __init__(self, bar_style=False):
        self._bar_style = bar_style

    @contextlib.contextmanager
    def session(self):
        yield ProgressRecorderSession(self._bar_style)


class ProgressRecorderSession(BaseRecorderSession):
    def __init__(self, bar_style):
        self._content_length = None
        self._bytes_received = 0
        self._response = None
        self._last_flush_time = 0
        self._bar_style = bar_style

    def request(self, request):
        print(
            _('Requesting {url}... ').format(url=request.url_info.url),
            end=''
        )
        sys.stdout.flush()

    def pre_response(self, response):
        print(response.status_code, response.status_reason)

        content_length = response.fields.get('Content-Length')

        if content_length:
            self._content_length = int(content_length)

        print(
            _('Length: {content_length} [{content_type}]').format(
                content_length=self._content_length,
                content_type=response.fields.get('Content-Type')
            )
        )

        self._response = response

    def response_data(self, data):
        if not self._response:
            return

        self._bytes_received += len(data)

        if self._bar_style and self._content_length:
            self._print_bar()
        else:
            self._print_dots()

    def _print_bar(self):
        # TODO:
        pass

    def _print_dots(self):
        time_now = time.time()

        if time_now - self._last_flush_time > 2.0:
            print('.', end='')
            sys.stdout.flush()
            self._last_flush_time = time.time()

    def response(self, response):
        print()
        print(_('Bytes received: {bytes_received}').format(
            bytes_received=self._bytes_received))
