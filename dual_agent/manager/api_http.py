"""Small HTTPS-only client: no redirects, proxies, SDK retries or key discovery.

DNS alone uses a daemon resolver (it cannot send HTTP). All socket/TLS I/O is
nonblocking on the caller and polled with CallControl. Cancellation closes the
socket before returning; a transmitted request remains possibly chargeable.
"""
from dataclasses import dataclass, field
import errno
import ipaddress
import os
import queue
import select
import socket
import ssl
import threading
from urllib.parse import urlsplit
from .provider import ProviderCancelled, ProviderTimeout
from .api_profiles import require_profile


class HTTPFailure(RuntimeError):
    def __init__(self,kind,sent=False):
        self.kind=kind;self.sent=sent
        super().__init__('API HTTP '+kind)


@dataclass(frozen=True)
class HTTPResult:
    status: int
    body: bytes = field(repr=False)
    request_id: str | None = None


def _wait(sock,control,write=False):
    while True:
        control.check()
        r,w,_=select.select([] if write else [sock],[sock] if write else [],[],.05)
        if r or w:return


def _resolve(host,control):
    result=queue.Queue(maxsize=1)
    def lookup():
        try:result.put(socket.getaddrinfo(host,443,type=socket.SOCK_STREAM))
        except OSError:result.put(None)
    threading.Thread(target=lookup,name='manager-dns',daemon=True).start()
    while True:
        control.check()
        try:addresses=result.get(timeout=.05);break
        except queue.Empty:continue
    if not addresses:raise HTTPFailure('dns')
    if any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):raise HTTPFailure('nonpublic_address')
    return addresses[0]  # Deliberately no automatic address/request retry.


def _tls_context():
    # Ignore SSL_CERT_FILE/SSL_CERT_DIR and project-provided CA settings.
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if os.name=='nt':
        for store in ('ROOT','CA'):
            for cert,encoding,trust in ssl.enum_certificates(store):
                if encoding=='x509_asn' and (trust is True or ssl.Purpose.SERVER_AUTH.oid in trust):
                    context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(cert))
    else:
        defaults=ssl.get_default_verify_paths()
        context.load_verify_locations(cafile=defaults.openssl_cafile,capath=defaults.openssl_capath)
    return context


def _chunked(data,limit):
    pos=0;body=bytearray()
    while True:
        end=data.find(b'\r\n',pos)
        if end<0:return None
        token=data[pos:end]
        if not token or len(token)>16 or any(c not in b'0123456789abcdefABCDEF' for c in token):raise HTTPFailure('framing',True)
        size=int(token,16);pos=end+2
        if len(body)+size>limit:raise HTTPFailure('response_limit',True)
        if size==0:
            if len(data)<pos+2:return None
            if data[pos:]!=b'\r\n':raise HTTPFailure('trailers_or_extra_data',True)
            return bytes(body)
        if len(data)<pos+size+2:return None
        if data[pos+size:pos+size+2]!=b'\r\n':raise HTTPFailure('framing',True)
        body.extend(data[pos:pos+size]);pos+=size+2


def _message(raw,limit,eof=False):
    at=raw.find(b'\r\n\r\n')
    if at<0:
        if len(raw)>16000 or eof:raise HTTPFailure('headers',True)
        return None
    if at>16000:raise HTTPFailure('header_limit',True)
    lines=raw[:at].split(b'\r\n')
    first=lines.pop(0).split(b' ',2)
    if len(first)<2 or first[0] not in {b'HTTP/1.0',b'HTTP/1.1'} or len(first[1])!=3 or not first[1].isdigit():raise HTTPFailure('status',True)
    status=int(first[1]);headers={}
    for line in lines:
        key,sep,value=line.partition(b':');key=key.lower()
        if not sep or not key or key in headers or any(c<=32 or c>=127 for c in key):raise HTTPFailure('headers',True)
        headers[key]=value.strip()
    if headers.get(b'content-encoding',b'identity')!=b'identity':raise HTTPFailure('encoding',True)
    if b'content-length' in headers and b'transfer-encoding' in headers:raise HTTPFailure('framing',True)
    body=raw[at+4:]
    if b'transfer-encoding' in headers:
        if headers[b'transfer-encoding'].lower()!=b'chunked':raise HTTPFailure('framing',True)
        body=_chunked(body,limit)
        if body is None:
            if eof:raise HTTPFailure('response_disconnected',True)
            return None
    elif b'content-length' in headers:
        size=headers[b'content-length']
        if not size.isdigit() or len(size)>12:raise HTTPFailure('framing',True)
        size=int(size)
        if size>limit:raise HTTPFailure('response_limit',True)
        if len(body)<size:
            if eof:raise HTTPFailure('response_disconnected',True)
            return None
        if len(body)!=size:raise HTTPFailure('extra_data',True)
    elif not eof:return None
    if len(body)>limit:raise HTTPFailure('response_limit',True)
    if status==200 and not headers.get(b'content-type',b'').lower().startswith(b'application/json'):
        raise HTTPFailure('content_type',True)
    request_id=headers.get(b'x-request-id')
    if request_id is not None:
        if not 1<=len(request_id)<=200 or any(c<33 or c>126 for c in request_id):raise HTTPFailure('request_id',True)
        request_id=request_id.decode('ascii')
    return HTTPResult(status,body,request_id)


class OfficialHTTPSClient:
    """Only invoked by APIManagerTransport after an official_https v3 grant."""
    def exchange(self,profile,body,credential,control):
        require_profile(profile)
        parts=urlsplit(profile.endpoint);sock=None;sent=False
        if parts.scheme!='https' or parts.username or parts.password or parts.port is not None or parts.query or parts.fragment:
            raise HTTPFailure('endpoint')
        if not isinstance(credential,str) or not 1<=len(credential)<=4096 or any(ord(c)<33 or ord(c)>126 for c in credential):
            raise HTTPFailure('credential')
        if not isinstance(body,bytes) or len(body)>profile.max_request_bytes:raise HTTPFailure('request_limit')
        phase='dns'
        try:
            control.check()
            family,kind,proto,_,address=_resolve(parts.hostname,control)
            phase='connect';sock=socket.socket(family,kind,proto);sock.setblocking(False)
            result=sock.connect_ex(address)
            if result not in {0,errno.EINPROGRESS,errno.EWOULDBLOCK,errno.EALREADY,10035}:raise HTTPFailure('connect')
            _wait(sock,control,True)
            if sock.getsockopt(socket.SOL_SOCKET,socket.SO_ERROR):raise HTTPFailure('connect')
            phase='tls'
            ctx=_tls_context()  # CERT_REQUIRED + check_hostname; OS roots only.
            sock=ctx.wrap_socket(sock,server_hostname=parts.hostname,do_handshake_on_connect=False)
            while True:
                control.check()
                try:sock.do_handshake();break
                except ssl.SSLWantReadError:_wait(sock,control)
                except ssl.SSLWantWriteError:_wait(sock,control,True)
            headers=('POST '+parts.path+' HTTP/1.1\r\nHost: '+parts.hostname+
                '\r\nAuthorization: Bearer '+credential+'\r\nContent-Type: application/json\r\nAccept: application/json'+
                '\r\nAccept-Encoding: identity\r\nConnection: close\r\nContent-Length: '+str(len(body))+'\r\n\r\n').encode('ascii')
            wire=headers+body;pos=0;phase='send'
            while pos<len(wire):
                control.check()
                try:
                    sent=True  # Any attempted write may have reached the peer.
                    count=sock.send(wire[pos:])
                    if not count:raise HTTPFailure('send_interrupted',True)
                    pos+=count
                except ssl.SSLWantReadError:_wait(sock,control)
                except (ssl.SSLWantWriteError,BlockingIOError):_wait(sock,control,True)
            phase='response';raw=bytearray()
            while True:
                control.check()
                try:block=sock.recv(4096)
                except ssl.SSLWantReadError:_wait(sock,control);continue
                except ssl.SSLWantWriteError:_wait(sock,control,True);continue
                except BlockingIOError:_wait(sock,control);continue
                raw.extend(block)
                if len(raw)>profile.max_response_bytes+16004:raise HTTPFailure('response_limit',True)
                result=_message(bytes(raw),profile.max_response_bytes,eof=not block)
                if result is not None:return result
        except (ProviderCancelled,ProviderTimeout) as exc:
            raise HTTPFailure('cancelled' if isinstance(exc,ProviderCancelled) else 'timeout',sent) from None
        except HTTPFailure:raise
        except (OSError,ValueError):raise HTTPFailure(phase,sent) from None
        finally:
            if sock is not None:sock.close()
