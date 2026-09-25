"""Small SIP/UDP + RTP/PCMU gateway to the existing ChatGPT Web Voice API.

One call at a time keeps RTP port ownership and conversation state unambiguous.
Bind to loopback unless an operator explicitly configures trusted peer IPs.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import ipaddress
import json
import os
import random
import re
import struct
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


def decode_pcmu(payload: bytes) -> bytes:
    """Decode G.711 µ-law octets to little-endian signed PCM16."""
    samples = bytearray()
    for octet in payload:
        value = (~octet) & 0xFF
        magnitude = (((value & 0x0F) << 3) + 0x84) << ((value & 0x70) >> 4)
        sample = 0x84 - magnitude if value & 0x80 else magnitude - 0x84
        samples.extend(struct.pack("<h", sample))
    return bytes(samples)


def encode_pcmu(pcm: bytes) -> bytes:
    """Encode little-endian signed PCM16 into G.711 µ-law."""
    encoded = bytearray()
    for (sample,) in struct.iter_unpack("<h", pcm[: len(pcm) & ~1]):
        sign = 0x80 if sample < 0 else 0
        magnitude = min(32635, abs(sample)) + 0x84
        segment = max(0, magnitude.bit_length() - 8)
        segment = min(segment, 7)
        encoded.append((~(sign | (segment << 4) | ((magnitude >> (segment + 3)) & 0x0F))) & 0xFF)
    return bytes(encoded)


def pcm_rms(pcm: bytes) -> float:
    samples = struct.iter_unpack("<h", pcm[: len(pcm) & ~1])
    total = 0
    count = 0
    for (sample,) in samples:
        total += sample * sample
        count += 1
    return (total / count) ** 0.5 if count else 0.0


def parse_rtp(data: bytes) -> tuple[int, int, bytes] | None:
    if len(data) < 12 or data[0] >> 6 != 2 or data[1] & 0x7F != 0:
        return None
    header_length = 12 + 4 * (data[0] & 0x0F)
    if len(data) < header_length:
        return None
    if data[0] & 0x10:
        if len(data) < header_length + 4:
            return None
        header_length += 4 + 4 * int.from_bytes(data[header_length + 2 : header_length + 4], "big")
    if header_length > len(data):
        return None
    padding = data[-1] if data[0] & 0x20 else 0
    if padding > len(data) - header_length:
        return None
    return (int.from_bytes(data[2:4], "big"), int.from_bytes(data[4:8], "big"),
            data[header_length : len(data) - padding if padding else len(data)])


def make_rtp(sequence: int, timestamp: int, ssrc: int, payload: bytes) -> bytes:
    return struct.pack("!BBHII", 0x80, 0, sequence & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc) + payload


@dataclass(slots=True)
class SipRequest:
    method: str
    headers: dict[str, str]
    body: str

    @property
    def call_id(self) -> str:
        return self.headers.get("call-id", "")


def parse_sip(data: bytes) -> SipRequest | None:
    if len(data) > 16_384:
        return None
    head, separator, body = data.partition(b"\r\n\r\n")
    if not separator:
        return None
    lines = head.decode("latin-1").split("\r\n")
    if not re.fullmatch(r"[A-Z]+\s+\S+\s+SIP/2\.0", lines[0]):
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if not colon:
            return None
        headers[name.strip().lower()] = value.strip()
    if any(name not in headers for name in ("via", "from", "to", "call-id", "cseq")):
        return None
    return SipRequest(lines[0].split()[0], headers, body.decode("latin-1"))


def sip_audio_endpoint(request: SipRequest, source_ip: str) -> tuple[str, int] | None:
    """Accept PCMU from the signalling peer, including private Docker NAT hops."""
    address = re.search(r"(?m)^c=IN IP4 ([^\r\n]+)", request.body)
    media = re.search(r"(?m)^m=audio (\d+) RTP/AVP ([0-9 ]+)", request.body)
    if not address or not media or "0" not in media.group(2).split():
        return None
    try:
        ip = ipaddress.ip_address(address.group(1).strip())
        port = int(media.group(1))
        peer_ip = ipaddress.ip_address(source_ip)
        same_private_hop = (
            ip.is_private
            and peer_ip.is_private
            and not (ip.is_loopback and peer_ip.is_loopback)
        )
        if (str(ip) != source_ip and not same_private_hop) or not 1 <= port <= 65535:
            return None
    except ValueError:
        return None
    return source_ip, port


@dataclass(slots=True)
class Call:
    request: SipRequest
    sip_peer: tuple[str, int]
    rtp_peer: tuple[str, int]
    tag: str = field(default_factory=lambda: f"{random.getrandbits(64):016x}")
    peer: Any = None
    input_track: Any = None
    bridge_session_id: str | None = None
    task: asyncio.Task[Any] | None = None
    output_task: asyncio.Task[Any] | None = None
    last_response: bytes | None = None
    established: bool = False
    established_event: asyncio.Event = field(default_factory=asyncio.Event)
    ack_event: asyncio.Event = field(default_factory=asyncio.Event)
    retransmit_task: asyncio.Task[Any] | None = None
    reconnect_task: asyncio.Task[Any] | None = None
    retries: int = 0
    sequence: int = field(default_factory=lambda: random.getrandbits(16))
    timestamp: int = field(default_factory=lambda: random.getrandbits(32))
    ssrc: int = field(default_factory=lambda: random.getrandbits(32))
    last_input_sequence: int | None = None
    last_rtp_at: float = field(default_factory=time.monotonic)
    last_voice_at: float = field(default_factory=time.monotonic)
    rtp_watchdog_task: asyncio.Task[Any] | None = None


class RtpInputTrack:
    """Create an aiortc audio track paced at 20 ms from bounded RTP packets."""

    def __init__(self) -> None:
        from aiortc import MediaStreamTrack

        class Track(MediaStreamTrack):
            kind = "audio"

            def __init__(self) -> None:
                super().__init__()
                self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
                self.start_time: float | None = None
                self.samples_sent = 0

            async def recv(self):
                from av import AudioFrame

                loop = asyncio.get_running_loop()
                if self.start_time is None:
                    self.start_time = loop.time()
                target = self.start_time + self.samples_sent / 8000
                await asyncio.sleep(max(0, target - loop.time()))
                try:
                    packet = self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    packet = b"\xff" * 160
                pcm = decode_pcmu(packet[:160].ljust(160, b"\xff"))
                frame = AudioFrame(format="s16", layout="mono", samples=160)
                frame.planes[0].update(pcm)
                frame.sample_rate = 8000
                frame.pts = self.samples_sent
                frame.time_base = Fraction(1, 8000)
                self.samples_sent += 160
                return frame

        self.track = Track()

    def push(self, payload: bytes) -> None:
        queue = self.track.queue
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(payload)


class _Datagrams(asyncio.DatagramProtocol):
    def __init__(self, callback: Any) -> None:
        self.callback = callback

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.callback(data, addr)


class SipGateway:
    def __init__(
        self, *, bridge_url: str, api_key: str, listen_ip: str, advertise_ip: str,
        sip_port: int, rtp_port: int, allowed_ips: tuple[str, ...],
        voice: str, model: str, project: str | None, conversation_id: str | None,
        text: str | None, files: list[dict[str, str]],
    ) -> None:
        self.bridge_url = bridge_url.rstrip("/")
        self.api_key = api_key
        self.listen_ip = listen_ip
        self.advertise_ip = advertise_ip
        self.sip_port = sip_port
        self.rtp_port = rtp_port
        self.allowed = tuple(ipaddress.ip_network(value, strict=False) for value in allowed_ips)
        self.voice = voice
        self.model = model
        self.project = project
        self.conversation_id = conversation_id
        self.text = text
        self.files = files
        self.sip_transport: asyncio.DatagramTransport | None = None
        self.rtp_transport: asyncio.DatagramTransport | None = None
        self.call: Call | None = None

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self.sip_transport, _ = await loop.create_datagram_endpoint(
            lambda: _Datagrams(self._sip_packet), local_addr=(self.listen_ip, self.sip_port)
        )
        self.rtp_transport, _ = await loop.create_datagram_endpoint(
            lambda: _Datagrams(self._rtp_packet), local_addr=(self.listen_ip, self.rtp_port)
        )
        print(f"SIP gateway listening on {self.listen_ip}:{self.sip_port}/udp; RTP {self.rtp_port}/udp", flush=True)
        try:
            await asyncio.Event().wait()
        finally:
            await self._end_call()
            self.sip_transport.close()
            self.rtp_transport.close()

    def _sip_packet(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            allowed = any(ipaddress.ip_address(addr[0]) in network for network in self.allowed)
        except ValueError:
            return
        if not allowed:
            return
        request = parse_sip(data)
        if not request:
            return
        if request.method == "OPTIONS":
            self._reply(request, addr, 200, "OK", extra={"Allow": "INVITE, ACK, BYE, CANCEL, OPTIONS, REGISTER"})
        elif request.method == "REGISTER":
            headers = {"Expires": request.headers.get("expires", "3600")}
            if request.headers.get("contact"):
                headers["Contact"] = request.headers["contact"]
            self._reply(request, addr, 200, "OK", extra=headers)
        elif request.method == "INVITE":
            if self.call:
                if request.call_id == self.call.request.call_id and self.call.last_response:
                    self.sip_transport.sendto(self.call.last_response, addr)
                else:
                    self._reply(request, addr, 486, "Busy Here")
                return
            media = sip_audio_endpoint(request, addr[0])
            if not media:
                self._reply(request, addr, 488, "Not Acceptable Here")
                return
            call = Call(request, addr, media)
            self.call = call
            call.last_response = self._reply(request, addr, 100, "Trying")
            call.task = asyncio.create_task(self._start_call(call))
        elif request.method == "ACK":
            if self.call and request.call_id == self.call.request.call_id:
                self.call.ack_event.set()
        elif request.method == "CANCEL":
            if self.call and request.call_id == self.call.request.call_id and not self.call.established:
                self._reply(request, addr, 200, "OK")
                self._reply(self.call.request, self.call.sip_peer, 487, "Request Terminated", tag=self.call.tag)
                asyncio.create_task(self._end_call())
            else:
                self._reply(request, addr, 481, "Call Does Not Exist")
        elif request.method == "BYE":
            if self.call and request.call_id == self.call.request.call_id and self.call.established:
                self._reply(request, addr, 200, "OK", tag=self.call.tag)
                asyncio.create_task(self._end_call())
            else:
                self._reply(request, addr, 481, "Call Does Not Exist")
        else:
            self._reply(request, addr, 405, "Method Not Allowed")

    def _rtp_packet(self, data: bytes, addr: tuple[str, int]) -> None:
        call = self.call
        if not call or not call.input_track or addr[0] != call.rtp_peer[0]:
            return
        parsed = parse_rtp(data)
        if not parsed:
            return
        sequence, _, payload = parsed
        if call.last_input_sequence is not None:
            delta = (sequence - call.last_input_sequence) & 0xFFFF
            if delta == 0 or delta >= 0x8000:
                return
        call.last_input_sequence = sequence
        call.last_rtp_at = time.monotonic()
        if pcm_rms(decode_pcmu(payload)) >= 300:
            call.last_voice_at = call.last_rtp_at
        call.rtp_peer = addr  # symmetric RTP, restricted to the approved SIP peer IP
        call.input_track.push(payload)

    async def _start_call(self, call: Call) -> None:
        try:
            await self._connect_upstream(call)
            if self.call is not call:
                return
            sdp = (
                f"v=0\r\no=- 0 0 IN IP4 {self.advertise_ip}\r\ns=ChatGPT Voice\r\n"
                f"c=IN IP4 {self.advertise_ip}\r\nt=0 0\r\n"
                f"m=audio {self.rtp_port} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\na=ptime:20\r\na=sendrecv\r\n"
            )
            call.last_response = self._reply(call.request, call.sip_peer, 200, "OK", sdp=sdp, tag=call.tag)
            call.established = True
            call.established_event.set()
            call.last_voice_at = time.monotonic()
            call.retransmit_task = asyncio.create_task(self._retransmit_answer(call))
            call.rtp_watchdog_task = asyncio.create_task(self._watch_rtp(call))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"SIP call setup failed: {type(exc).__name__}", flush=True)
            if self.call is call:
                self._reply(call.request, call.sip_peer, 502, "Bad Gateway")
                await self._end_call()

    async def _connect_upstream(self, call: Call) -> None:
        from aiortc import RTCPeerConnection, RTCSessionDescription

        if call.output_task:
            call.output_task.cancel()
        if call.peer:
            await call.peer.close()
        pc = RTCPeerConnection()
        call.peer = pc
        connected = asyncio.Event()
        call.input_track = RtpInputTrack()
        pc.addTrack(call.input_track.track)
        pc.createDataChannel("oai-events", negotiated=True, id=0)

        @pc.on("track")
        def on_track(track: Any) -> None:
            if track.kind == "audio":
                call.output_task = asyncio.create_task(self._send_audio(call, track))

        @pc.on("connectionstatechange")
        def on_connection_state() -> None:
            if pc.connectionState == "connected":
                connected.set()
            if self.call is not call or not call.established or call.peer is not pc:
                return
            if pc.connectionState == "failed" and not call.reconnect_task:
                call.reconnect_task = asyncio.create_task(self._reconnect(call))
            elif pc.connectionState == "disconnected" and not call.reconnect_task:
                call.reconnect_task = asyncio.create_task(self._reconnect(call, delay=4))

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        request = {"offer_sdp": pc.localDescription.sdp, "voice": self.voice}
        if call.bridge_session_id:
            request["bridge_session_id"] = call.bridge_session_id
        else:
            request.update({"model": self.model, "project": self.project,
                            "conversation_id": self.conversation_id,
                            "text": self.text, "files": self.files})
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                self.bridge_url + "/v1/chatgpt/voice/sessions", json=request,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        response.raise_for_status()
        result = response.json()
        call.bridge_session_id = result["bridge_session_id"]
        answer = re.sub(r"(?m)^a=sctp-init:[^\r\n]*(?:\r?\n|$)", "", result["answer_sdp"])
        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
        await asyncio.wait_for(connected.wait(), timeout=20)

    async def _reconnect(self, call: Call, delay: float = 0) -> None:
        try:
            if delay:
                await asyncio.sleep(delay)
                if call.peer.connectionState == "connected":
                    return
            while self.call is call and call.retries < 2:
                call.retries += 1
                await asyncio.sleep(call.retries)
                try:
                    await self._connect_upstream(call)
                    return
                except Exception as exc:
                    print(f"WebRTC reconnect failed: {type(exc).__name__}", flush=True)
                    continue
            if self.call is call:
                self._send_bye(call)
                await self._end_call()
        finally:
            if self.call is call:
                call.reconnect_task = None

    def _send_bye(self, call: Call) -> None:
        caller = re.search(r"<([^>]+)>", call.request.headers["from"])
        uri = caller.group(1) if caller else f"sip:{call.sip_peer[0]}"
        to_value = call.request.headers["to"]
        if ";tag=" not in to_value:
            to_value += ";tag=" + call.tag
        bye = (
            f"BYE {uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.advertise_ip}:{self.sip_port};branch=z9hG4bK{random.getrandbits(64):016x}\r\n"
            f"From: {to_value}\r\nTo: {call.request.headers['from']}\r\n"
            f"Call-ID: {call.request.call_id}\r\nCSeq: 2 BYE\r\nContent-Length: 0\r\n\r\n"
        ).encode("ascii", "ignore")
        self.sip_transport.sendto(bye, call.sip_peer)

    async def _retransmit_answer(self, call: Call) -> None:
        for delay in (0.5, 1, 2, 4, 4):
            try:
                await asyncio.wait_for(call.ack_event.wait(), timeout=delay)
                return
            except TimeoutError:
                if self.call is not call:
                    return
                self.sip_transport.sendto(call.last_response, call.sip_peer)
        if self.call is call:
            await self._end_call()

    async def _watch_rtp(self, call: Call) -> None:
        while self.call is call and call.established:
            await asyncio.sleep(1)
            now = time.monotonic()
            if now - call.last_rtp_at >= 30 or now - call.last_voice_at >= 30:
                print("SIP call ended after 30 seconds without client or voice activity", flush=True)
                self._send_bye(call)
                await self._end_call()
                return

    async def _send_audio(self, call: Call, track: Any) -> None:
        from aiortc.mediastreams import MediaStreamError
        from av.audio.resampler import AudioResampler

        resampler = AudioResampler(format="s16", layout="mono", rate=8000)
        pending = bytearray()
        try:
            await call.established_event.wait()
            while self.call is call:
                frame = await track.recv()
                for output in resampler.resample(frame):
                    pending.extend(bytes(output.planes[0])[: output.samples * 2])
                while len(pending) >= 320:
                    chunk = bytes(pending[:320])
                    del pending[:320]
                    if pcm_rms(chunk) >= 300:
                        call.last_voice_at = time.monotonic()
                    packet = make_rtp(call.sequence, call.timestamp, call.ssrc, encode_pcmu(chunk))
                    self.rtp_transport.sendto(packet, call.rtp_peer)
                    call.sequence = (call.sequence + 1) & 0xFFFF
                    call.timestamp = (call.timestamp + 160) & 0xFFFFFFFF
        except asyncio.CancelledError:
            pass
        except (EOFError, MediaStreamError):
            if self.call is call and call.established:
                print("ChatGPT ended the remote audio track", flush=True)
                self._send_bye(call)
                await self._end_call()

    async def _end_call(self) -> None:
        call = self.call
        if not call:
            return
        self.call = None
        if call.task and call.task is not asyncio.current_task():
            call.task.cancel()
        if call.output_task:
            if call.output_task is not asyncio.current_task():
                call.output_task.cancel()
        if call.retransmit_task and call.retransmit_task is not asyncio.current_task():
            call.retransmit_task.cancel()
        if call.reconnect_task and call.reconnect_task is not asyncio.current_task():
            call.reconnect_task.cancel()
        if call.rtp_watchdog_task and call.rtp_watchdog_task is not asyncio.current_task():
            call.rtp_watchdog_task.cancel()
        if call.peer:
            await call.peer.close()
        if call.bridge_session_id:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(
                        self.bridge_url + "/v1/chatgpt/voice/sessions/release",
                        json={"bridge_session_id": call.bridge_session_id},
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    )
            except httpx.HTTPError:
                pass

    def _reply(
        self, request: SipRequest, addr: tuple[str, int], status: int, reason: str, *,
        sdp: str = "", tag: str | None = None, extra: dict[str, str] | None = None,
    ) -> bytes:
        to_value = request.headers["to"]
        if tag and ";tag=" not in to_value:
            to_value += ";tag=" + tag
        payload = sdp.encode("ascii")
        lines = [
            f"SIP/2.0 {status} {reason}",
            f"Via: {request.headers['via']}",
            f"From: {request.headers['from']}",
            f"To: {to_value}",
            f"Call-ID: {request.call_id}",
            f"CSeq: {request.headers['cseq']}",
        ]
        response_headers = extra or {}
        if not any(name.lower() == "contact" for name in response_headers):
            lines.append(f"Contact: <sip:voice@{self.advertise_ip}:{self.sip_port}>")
        for name, value in response_headers.items():
            lines.append(f"{name}: {value}")
        if sdp:
            lines.append("Content-Type: application/sdp")
        lines.extend((f"Content-Length: {len(payload)}", "", ""))
        response = "\r\n".join(lines).encode("ascii") + payload
        self.sip_transport.sendto(response, addr)
        return response


def main() -> None:
    parser = argparse.ArgumentParser(description="One-call SIP/RTP PCMU gateway for ChatGPT Web Voice")
    parser.add_argument("--bridge-url", default=os.environ.get("CHATGPT_SIP_BRIDGE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--listen-ip", default=os.environ.get("CHATGPT_SIP_LISTEN_IP", "127.0.0.1"))
    parser.add_argument("--advertise-ip", default=os.environ.get("CHATGPT_SIP_ADVERTISE_IP"))
    parser.add_argument("--sip-port", type=int, default=int(os.environ.get("CHATGPT_SIP_PORT", "5060")))
    parser.add_argument("--rtp-port", type=int, default=int(os.environ.get("CHATGPT_SIP_RTP_PORT", "40000")))
    parser.add_argument("--allow-ip", action="append", dest="allowed_ips")
    parser.add_argument("--voice", default=os.environ.get("CHATGPT_SIP_VOICE", "arbor"))
    parser.add_argument("--model", default=os.environ.get("CHATGPT_SIP_MODEL", "auto"))
    parser.add_argument("--project", default=os.environ.get("CHATGPT_SIP_PROJECT"))
    parser.add_argument("--conversation-id", default=os.environ.get("CHATGPT_SIP_CONVERSATION_ID"))
    parser.add_argument("--text", default=os.environ.get("CHATGPT_SIP_TEXT"))
    parser.add_argument("--attachment", action="append", default=[], type=Path)
    args = parser.parse_args()
    key = os.environ.get("CHATGPT_API_KEY")
    if not key:
        parser.error("set CHATGPT_API_KEY for the local bridge")
    bridge = urlparse(args.bridge_url)
    if bridge.scheme not in {"http", "https"} or not bridge.hostname:
        parser.error("--bridge-url must be an HTTP(S) URL")
    if bridge.scheme == "http" and bridge.hostname not in {"127.0.0.1", "localhost", "::1", "chatgpt-api"}:
        parser.error("remote --bridge-url requires HTTPS to protect the bridge key")
    if args.listen_ip == "0.0.0.0" and not args.advertise_ip:
        parser.error("--advertise-ip is required when listening on all interfaces")
    if len(args.attachment) > 10 or sum(path.stat().st_size for path in args.attachment) > 25 * 1024 * 1024:
        parser.error("attachments exceed 10 files or 25 MiB total")
    files = []
    for path in args.attachment:
        if path.suffix.lower() not in {".txt", ".md", ".csv", ".json"} or path.stat().st_size > 20 * 1024 * 1024:
            parser.error("attachments must be supported text files of at most 20 MiB")
        contents = path.read_bytes()
        try:
            contents.decode("utf-8-sig")
        except UnicodeDecodeError:
            parser.error("attachments must be UTF-8 text")
        files.append({"filename": path.name, "file_data": base64.b64encode(contents).decode("ascii")})
    if files and not args.text:
        parser.error("--text is required with --attachment")
    gateway = SipGateway(
        bridge_url=args.bridge_url, api_key=key, listen_ip=args.listen_ip,
        advertise_ip=args.advertise_ip or args.listen_ip,
        sip_port=args.sip_port, rtp_port=args.rtp_port,
        allowed_ips=tuple(args.allowed_ips or [
            value.strip() for value in os.environ.get("CHATGPT_SIP_ALLOWED_IPS", "127.0.0.1/32").split(",") if value.strip()
        ]),
        voice=args.voice, model=args.model, project=args.project,
        conversation_id=args.conversation_id, text=args.text, files=files,
    )
    try:
        asyncio.run(gateway.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
